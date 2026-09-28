"""Session and sign-in health for the ``/ui`` control surface: see what's
broken and fix it from the page, without a terminal.

A local Copilot session whose credentials expired keeps its process alive and
stays registered, but every model call fails, so it silently ignores messages.
A venue worker borrows this machine's git/Azure sign-ins through the credential
relay, so an expired one there fails every worker at once. These routes find
both and let the page repair them:

- ``GET  /api/v1/ui/session-problems`` -- for each live local session, the
  latest error that no later model activity has cleared (from
  ``~/.copilot/session-state/<id>/events.jsonl``).
- ``GET  /api/v1/ui/host-auth`` -- whether the sign-ins the relay hands to
  workers still work here: Git Credential Manager for the relay's Azure DevOps
  host, and the Azure CLI. Checked without prompting, cached, refreshed in the
  background.
- ``POST|GET|DELETE /api/v1/ui/sign-in/{provider}`` -- run the provider's own
  sign-in on this machine and report how it went: ``copilot`` (a GitHub device
  code), ``azure`` (``az login``), ``ado`` (Git Credential Manager's sign-in).
  Each opens its browser window itself on a desktop.
- ``POST /api/v1/ui/tasks/{worktree_id}/restart`` -- ``agent-worktrees restart``
  then ``embody --resume``: the same session, its history kept, with this
  machine's current sign-in. The bridge follows it back and tells it what the
  restart cost it (its background shells), plus an optional message.

The write routes need a same-origin request, as the other task verbs do.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..provider_sources import discover_provider_manifests
from . import live_sessions as _live
from . import ui_tasks as _tasks

router = APIRouter()

TAIL_BYTES = 512 * 1024
PROBLEMS_TTL = 5.0
LOGIN_CODE_TIMEOUT = 30.0
LOGIN_MAX_SECONDS = 15 * 60
RESTART_TIMEOUT = 90.0
#: How long a restarted session has to register again before the page is told.
RESUME_WAIT = 240.0
#: Host sign-in checks are reused this long when passing / failing (seconds).
HOST_AUTH_OK_TTL = 300.0
HOST_AUTH_BAD_TTL = 45.0
HOST_CHECK_TIMEOUT = 60.0
RELAY_PROFILE_TTL = 3600.0
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}$")
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{1,252}$")
_RESOURCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:/._-]{1,200}$")
_DEVICE = re.compile(r"(https://\S+/login/device)\S*\s+and enter (?:the )?code\s+([A-Z0-9]{4}-[A-Z0-9]{4})")
_AZ_DEVICE = re.compile(r"(https://\S*devicelogin)\S*\s+and enter the code\s+([A-Z0-9]{6,12})")
#: Log events after which an earlier error no longer applies: the model answered,
#: or the process restarted (fresh credentials); a new failure logs a new error.
_RECOVERED = ("assistant.message", "tool.execution_start", "assistant.turn_start",
              "model.model_call_success", "session.resume", "session.start")
#: The Azure DevOps resource the relay's Azure tokens are for.
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"


def session_state_root() -> Path:
    return Path.home() / ".copilot" / "session-state"


def latest_problem(session_id: str, root: Path | None = None) -> dict[str, Any] | None:
    """The newest ``session.error`` in a session's log that no later model
    activity cleared, or ``None``. Reads only the log's tail."""
    if not _SESSION_ID.match(session_id):
        return None
    path = (root or session_state_root()) / session_id / "events.jsonl"
    try:
        with path.open("rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - TAIL_BYTES))
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    problem: dict[str, Any] | None = None
    for line in tail.splitlines():
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        kind = rec.get("type")
        if kind == "session.error":
            d = rec.get("data") if isinstance(rec.get("data"), dict) else {}
            status = d.get("statusCode")
            auth = d.get("errorType") == "authorization" or status in (401, 403)
            problem = {
                "kind": "auth" if auth else "error",
                "message": str(d.get("message") or "The session hit an error.")[:500],
                "status_code": status if isinstance(status, int) else None,
                "at": rec.get("timestamp"),
            }
        elif kind in _RECOVERED:
            problem = None
    return problem


@router.get("/api/v1/ui/session-problems", include_in_schema=False)
async def session_problems(request: Request) -> dict[str, Any]:
    st = _tasks._state(request)
    hit = st.get("problems")
    if hit and time.time() - hit[0] < PROBLEMS_TTL:
        return hit[1]
    live = (await _live.list_live_sessions(request)).live_sessions
    root = session_state_root()
    ids = [s.session_id for s in live if s.status == "live" and not getattr(s, "venue", None)]
    found = await asyncio.gather(*(asyncio.to_thread(latest_problem, sid, root) for sid in ids))
    out = {"problems": {sid: p for sid, p in zip(ids, found) if p}, "checked": len(ids)}
    st["problems"] = (time.time(), out)
    return out


# -- running a command -----------------------------------------------------------


#: Tests replace this to fake the commands the sign-ins and checks run.
_spawn = asyncio.create_subprocess_exec


async def _run(argv: list[str], *, stdin: bytes | None = None, env: dict[str, str] | None = None,
               timeout: float) -> tuple[int, str, str]:
    proc = await _spawn(
        *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})})
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "", f"timed out after {int(timeout)}s"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


def _last_line(text: str, rc: int) -> str:
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip() and "clipboard" not in ln.lower()]
    return (lines[-1] if lines else f"exited {rc}")[:300]


def _credential_query(host: str) -> bytes:
    return f"protocol=https\nhost={host}\n\n".encode()


# -- what the relay hands to workers --------------------------------------------------


async def relay_profile(app: Any) -> dict[str, Any] | None:
    """The credential relay's policy (agent-codespaces ``relay-profile``): its
    Azure DevOps host and Azure resources. ``None`` without agent-codespaces."""
    st = _tasks.state_for(app)
    hit = st.get("relay_profile")
    if hit and time.time() - hit[0] < RELAY_PROFILE_TTL:
        return hit[1]
    manifest = discover_provider_manifests().get("codespace")
    if not manifest:
        return None
    rc, out, _err = await _run([*manifest.command, "relay-profile"], timeout=HOST_CHECK_TIMEOUT)
    try:
        profile = json.loads(out.strip().splitlines()[-1]) if rc == 0 and out.strip() else None
    except ValueError:
        profile = None
    if isinstance(profile, dict):
        st["relay_profile"] = (time.time(), profile)
        return profile
    return None


async def _check_ado(host: str) -> dict[str, Any]:
    git = shutil.which("git")
    if not git:
        return {"ok": False, "detail": "git isn't on this machine's PATH"}
    rc, out, err = await _run([git, "credential", "fill"], stdin=_credential_query(host),
                              env={"GCM_INTERACTIVE": "never", "GIT_TERMINAL_PROMPT": "0"},
                              timeout=HOST_CHECK_TIMEOUT)
    if rc == 0 and "\npassword=" in "\n" + out:
        return {"ok": True}
    return {"ok": False, "detail": _last_line(err, rc)}


async def _check_azure(resource: str) -> dict[str, Any]:
    az = shutil.which("az")
    if not az:
        return {"ok": False, "detail": "the Azure CLI isn't on this machine's PATH"}
    rc, _out, err = await _run([az, "account", "get-access-token", "--resource", resource, "-o", "none"],
                               timeout=HOST_CHECK_TIMEOUT)
    return {"ok": True} if rc == 0 else {"ok": False, "detail": _last_line(err, rc)}


async def check_host_auth(app: Any) -> list[dict[str, Any]]:
    profile = await relay_profile(app)
    if not profile:
        return []
    checks: list[tuple[str, str, Any]] = []
    host = str(profile.get("ado_host") or "")
    if "git-credential" in (profile.get("sources") or ["git-credential"]) and _HOST.match(host):
        checks.append(("ado", f"Azure DevOps sign-in ({host})", _check_ado(host)))
    resources = [r for r in profile.get("azure_resources") or [] if isinstance(r, str) and _RESOURCE.match(r)]
    if resources:
        resource = ADO_RESOURCE if ADO_RESOURCE in resources else resources[0]
        checks.append(("azure", "Azure CLI sign-in", _check_azure(resource)))
    results = await asyncio.gather(*(c for _, _, c in checks))
    now = time.time()
    return [{"kind": kind, "label": label, "checked_at": now, **res}
            for (kind, label, _), res in zip(checks, results)]


def _host_auth_state(app: Any) -> dict[str, Any]:
    st = _tasks.state_for(app)
    return st.setdefault("host_auth", {"checks": None, "at": 0.0, "task": None})


def _refresh_host_auth(app: Any) -> asyncio.Task:
    ha = _host_auth_state(app)
    if ha["task"] is None or ha["task"].done():
        async def run() -> None:
            ha["checks"] = await check_host_auth(app)
            ha["at"] = time.time()
        ha["task"] = asyncio.create_task(run(), name="ui-host-auth")
    return ha["task"]


def invalidate_host_auth(app: Any) -> None:
    _host_auth_state(app)["at"] = 0.0


@router.get("/api/v1/ui/host-auth", include_in_schema=False)
async def host_auth(request: Request, refresh: bool = False) -> dict[str, Any]:
    ha = _host_auth_state(request.app)
    checks = ha["checks"]
    ttl = HOST_AUTH_OK_TTL if checks and all(c["ok"] for c in checks) else HOST_AUTH_BAD_TTL
    if refresh or checks is None or time.time() - ha["at"] > ttl:
        task = _refresh_host_auth(request.app)
        if checks is None or refresh:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=HOST_CHECK_TIMEOUT + 10)
            except asyncio.TimeoutError:
                pass
    signins = _signins(request.app)
    return {"checks": ha["checks"] or [],
            "checking": ha["task"] is not None and not ha["task"].done(),
            "signins": {k: _public_signin(v) for k, v in signins.items()}}


# -- sign in ------------------------------------------------------------------------

PROVIDERS = {
    "copilot": "GitHub Copilot",
    "azure": "Azure CLI",
    "ado": "Azure DevOps (Git Credential Manager)",
}


def _signins(app: Any) -> dict[str, dict[str, Any]]:
    st = getattr(app.state, "ui_signins", None)
    if st is None:
        st = app.state.ui_signins = {}
    return st


def _public_signin(st: dict[str, Any]) -> dict[str, Any]:
    return {k: st.get(k) for k in ("status", "code", "url", "detail", "started_at", "finished_at")}


async def _pump(stream: asyncio.StreamReader | None, sink: list[str], st: dict[str, Any]) -> None:
    """Collect a sign-in's output, picking up a device code if one is offered."""
    if stream is None:
        return
    while True:
        line = await stream.readline()
        if not line:
            return
        sink.append(line.decode("utf-8", "replace"))
        if not st.get("code"):
            m = _AZ_DEVICE.search("".join(sink[-4:])) or _DEVICE.search("".join(sink[-4:]))
            if m:
                st.update(url=m.group(1), code=m.group(2))


async def _finish_signin(app: Any, kind: str, st: dict[str, Any], proc: asyncio.subprocess.Process,
                         out: list[str]) -> None:
    err: list[str] = []
    pumps = [asyncio.create_task(_pump(proc.stdout, out, st)), asyncio.create_task(_pump(proc.stderr, err, st))]
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=LOGIN_MAX_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        st.update(status="failed", detail="the sign-in wasn't completed in time", finished_at=time.time())
        return
    finally:
        await asyncio.gather(*pumps, return_exceptions=True)
    if st.get("proc") is not proc:
        return
    st.pop("proc", None)
    text = "".join(out)
    ok = rc == 0
    if ok and kind == "ado":
        ok = "\npassword=" in "\n" + text
        if ok:
            git = shutil.which("git")
            if git:  # store it, as git itself does after a credential works
                await _run([git, "credential", "approve"], stdin=text.encode(), timeout=HOST_CHECK_TIMEOUT)
    # Never report Git Credential Manager's stdout: it holds the credential.
    errors = "".join(err) if kind == "ado" else ("".join(err) or text)
    st.update(status="done" if ok else "failed", finished_at=time.time(), code=None, url=None,
              detail=None if ok else _last_line(errors, rc))
    _tasks.state_for(app).pop("problems", None)
    invalidate_host_auth(app)


async def start_signin(app: Any, kind: str) -> dict[str, Any] | str:
    """Start ``kind``'s sign-in (idempotent while one is waiting); its public
    state, or why it couldn't start."""
    signins = _signins(app)
    st = signins.setdefault(kind, {"status": "idle"})
    if st.get("status") == "waiting" and st.get("proc") is not None:
        return _public_signin(st)
    stdin: bytes | None = None
    env: dict[str, str] = {}
    if kind == "copilot":
        exe = shutil.which("copilot")
        argv = [exe, "login", "--device-code"] if exe else None
    elif kind == "azure":
        exe = shutil.which("az")
        argv = [exe, "login", "--output", "none"] if exe else None
    else:
        exe = shutil.which("git")
        profile = await relay_profile(app) or {}
        host = str(profile.get("ado_host") or "")
        if not _HOST.match(host):
            return "the credential relay names no Azure DevOps host to sign in to"
        argv = [exe, "credential", "fill"] if exe else None
        stdin = _credential_query(host)
        # The daemon usually inherits an agent shell's GCM_INTERACTIVE=never;
        # this one run is the operator asking to be prompted.
        env = {"GCM_INTERACTIVE": "auto", "GIT_TERMINAL_PROMPT": "0"}
    if not argv:
        return f"the {PROVIDERS[kind]} command isn't on this machine's PATH"
    proc = await _spawn(
        *argv, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={**os.environ, **env})
    if stdin is not None and proc.stdin is not None:
        proc.stdin.write(stdin)
        await proc.stdin.drain()
        proc.stdin.close()
    out: list[str] = []
    if kind == "copilot":
        # Copilot prints its code first thing; without one there's nothing to show.
        seen = ""
        deadline = time.monotonic() + LOGIN_CODE_TIMEOUT
        match = None
        while time.monotonic() < deadline and proc.stdout is not None:
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            if not line:
                break
            seen += line.decode("utf-8", "replace")
            match = _DEVICE.search(seen)
            if match:
                break
        if not match:
            proc.kill()
            return "copilot login didn't offer a device code: " + seen.strip()[-300:]
        out.append(seen)
    st.clear()
    st.update(status="waiting", proc=proc, started_at=time.time(), detail=None,
              code=match.group(2) if kind == "copilot" else None,
              url=match.group(1) if kind == "copilot" else None)
    st["watch"] = asyncio.create_task(_finish_signin(app, kind, st, proc, out), name=f"ui-sign-in-{kind}")
    return _public_signin(st)


@router.post("/api/v1/ui/sign-in/{kind}", include_in_schema=False)
async def post_signin(kind: str, request: Request) -> JSONResponse:
    if kind not in PROVIDERS:
        return _tasks._refuse(404, "unknown sign-in")
    if not _tasks._same_origin(request):
        return _tasks._refuse(403, "cross-origin requests may not start a sign-in")
    started = await start_signin(request.app, kind)
    if isinstance(started, str):
        return _tasks._refuse(502, started)
    return JSONResponse(started)


@router.get("/api/v1/ui/sign-in/{kind}", include_in_schema=False)
async def get_signin(kind: str, request: Request) -> JSONResponse:
    if kind not in PROVIDERS:
        return _tasks._refuse(404, "unknown sign-in")
    return JSONResponse(_public_signin(_signins(request.app).get(kind) or {"status": "idle"}))


@router.delete("/api/v1/ui/sign-in/{kind}", include_in_schema=False)
async def cancel_signin(kind: str, request: Request) -> JSONResponse:
    if kind not in PROVIDERS:
        return _tasks._refuse(404, "unknown sign-in")
    if not _tasks._same_origin(request):
        return _tasks._refuse(403, "cross-origin requests may not change a sign-in")
    st = _signins(request.app).setdefault(kind, {})
    proc = st.pop("proc", None)
    if proc is not None and proc.returncode is None:
        proc.kill()
    st.clear()
    st["status"] = "idle"
    return JSONResponse(_public_signin(st))


# -- restart --------------------------------------------------------------------------

#: What every resumed session is told: the restart ended its background work.
RESTART_NOTE = (
    "(Sent by the bridge UI.) This session was restarted{why} and resumed with its "
    "history. The restart stopped every background shell it had running, including any "
    "watcher or poll: restart the ones you still need, then carry on with what you were doing."
)


def restart_message(problem: dict[str, Any] | None, note: str) -> str:
    why = ""
    if problem:
        why = " because its Copilot sign-in had failed" if problem.get("kind") == "auth" \
            else f" after an error ({str(problem.get('message') or '')[:160]})"
    text = RESTART_NOTE.format(why=why)
    return text + ("\n\nThe operator's message:\n" + note if note else "")


@router.post("/api/v1/ui/tasks/{worktree_id}/restart", include_in_schema=False)
async def restart_task(worktree_id: str, request: Request) -> JSONResponse:
    """Stop a worktree's Copilot and resume the named session in it (its
    history kept, with this machine's current sign-in).

    ``embody`` alone starts a fresh Copilot (it is built for handoffs), so the
    session is resumed explicitly with ``--resume=<session_id>``. The session
    must be this worktree's and have its conversation on disk. The bridge sends
    the resumed session its message once it re-registers: typing a seed into a
    resuming pane is unreliable."""
    st, body, refused = await _tasks._guard(request)
    if refused:
        return refused
    cache = st.get("cache") or await _tasks._refresh(st)
    row = next((r for r in cache["workspaces"] if r.get("id") == worktree_id), None)
    if row is None:
        return _tasks._refuse(404, "unknown worktree")
    if row.get("status") != "active":
        return _tasks._refuse(409, f"this worktree is {row.get('status')}; only an active one can restart")
    session_id = str(body.get("session_id") or "").strip()
    if not _SESSION_ID.match(session_id):
        return _tasks._refuse(400, "name the session to resume")
    live = (await _live.list_live_sessions(request, worktree_id=worktree_id)).live_sessions
    current = next((s for s in live if s.session_id == session_id), None)
    if current is None:
        return _tasks._refuse(409, "that session isn't this worktree's live session")
    if not (session_state_root() / session_id / "events.jsonl").is_file():
        return _tasks._refuse(409, "that session has no saved conversation to resume")
    note = str(body.get("note") or "").strip()[:_tasks.MAX_PROMPT_CHARS]
    problem = await asyncio.to_thread(latest_problem, session_id)
    # Before anything stops: the resumed process re-registers under the same
    # id, so it is told apart by its new pid (and by registering after this).
    started = time.time()
    old_pid = current.pid
    async with st["lock"]:
        stopped, err = await _tasks._aw(["-p", row["project"], "restart", worktree_id, "--json"],
                                        timeout=RESTART_TIMEOUT)
        if stopped is None:
            return _tasks._refuse(502, "could not stop the session: " + (err or "agent-worktrees failed"))
        embodied, err = await _tasks._embody(row["project"], worktree_id, None,
                                             copilot_args=[f"--resume={session_id}"])
    _tasks._revalidate(st)
    st.pop("problems", None)
    if embodied is None or not embodied.get("ok", True):
        return _tasks._refuse(502, "stopped the session but could not resume it: "
                              + (err or str(_tasks._find(embodied, "error") or "embody failed")))
    outcome = {"state": "waiting", "session_id": session_id, "restarted_at": started,
               "note": "waiting", "with_message": bool(note)}
    _restarts(request)[worktree_id] = outcome
    outcome_task = asyncio.create_task(
        _follow_restart(request, worktree_id, session_id, started, old_pid, restart_message(problem, note)),
        name=f"ui-restart-{worktree_id}")
    _restarts(request)[worktree_id + ":task"] = outcome_task
    return JSONResponse({"worktree_id": worktree_id, "restarted": True, **outcome})


def _restarts(request: Request) -> dict[str, Any]:
    st = getattr(request.app.state, "ui_restarts", None)
    if st is None:
        st = request.app.state.ui_restarts = {}
    return st


def _came_back(s: Any, session_id: str, started: float, old_pid: int | None) -> bool:
    """Is this row the restarted process? Its pid changed, or (without a pid)
    it refreshed its registration after the restart began."""
    if s.session_id != session_id or s.status != "live":
        return False
    if old_pid is not None and s.pid is not None:
        return s.pid != old_pid
    return float(s.updated_at or 0) > started


async def _follow_restart(request: Request, worktree_id: str, session_id: str, started: float,
                          old_pid: int | None, message: str, *, timeout: float = RESUME_WAIT,
                          poll: float = 3.0) -> None:
    """Wait for the restarted worktree to register again. The same session
    back under a new process means it resumed: send it the message through the
    bridge. A different session registered since the restart means Copilot
    couldn't resume and started fresh, which is reported."""
    outcome = _restarts(request)[worktree_id]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await asyncio.sleep(poll)
        try:
            live = (await _live.list_live_sessions(request, worktree_id=worktree_id)).live_sessions
        except Exception:  # noqa: BLE001 -- a transient read; try again
            continue
        if any(_came_back(s, session_id, started, old_pid) for s in live):
            outcome["state"] = "resumed"
            try:
                await _live.post_live_message(session_id, _live.SendMessageRequest(
                    sender="bridge-ui", body=message, kind="prompt", delivery="steer",
                    expected_session_id=session_id,
                    idempotency_key=f"ui-restart:{session_id}:{int(started)}"), request)
                outcome["note"] = "sent"
            except Exception as exc:  # noqa: BLE001 -- reported on the page
                outcome.update(note="failed", note_detail=str(getattr(exc, "detail", exc))[:300])
            return
        other = next((s for s in live if s.session_id != session_id and s.status == "live"
                      and float(s.registered_at or 0) > started), None)
        if other is not None:
            outcome.update(state="fresh", new_session_id=other.session_id, note="not_sent")
            return
    outcome.update(state="timeout", note="not_sent")


@router.get("/api/v1/ui/tasks/{worktree_id}/restart", include_in_schema=False)
async def restart_status(worktree_id: str, request: Request) -> dict[str, Any]:
    return _restarts(request).get(worktree_id) or {"state": "none"}

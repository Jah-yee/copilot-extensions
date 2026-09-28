"""Venue workers for the ``/ui`` control surface: notice one that lost its
connection to this machine and bring it back.

A CodeSpace worker reaches the host over one SSH connection that the
agent-codespaces Connection Owner keeps up. Its bridge heartbeat and its
credential relay (the git/ADO sign-in it borrows from this machine) both ride
that connection. When the connection goes (the Owner exited, the CodeSpace
restarted), the worker keeps running but can't authenticate, and its row
quietly lapses, so nothing tells anyone. This module watches for that:

- **connected** -- registered and heartbeating.
- **lost** -- its heartbeat lapsed, or its row was reaped after lapsing. The
  bridge reconnects it: ``agent-codespaces copilot <codespace> --detach``
  from its supervisor's worktree, resuming the same conversation. That brings
  the Owner's forwards back, and restarts the worker (with its history) if the
  CodeSpace had restarted too. Attempts back off; after the last one the page
  offers the same thing as a button.
- **stopped** -- its row was removed while it was still heartbeating: ``copilot
  --stop`` or an exit deregisters it. That is deliberate and is left alone.

Only a worker this machine supervises is repaired, and only while its
supervisor's worktree is active. Workers are remembered for a day in
``ui-workers.json`` under the bridge's config directory, so one whose row was
reaped is still recognised.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..install_paths import effective_config_dir
from ..provider_sources import discover_provider_manifests
from . import ui_tasks as _tasks

log = logging.getLogger("agent-bridge")

router = APIRouter()

#: A worker whose heartbeat is older than this has lost its connection (the
#: extension beats every ~30s; the bridge's own lease is 120s).
STALE_AFTER = 150.0
SWEEP_INTERVAL = 45.0
REMEMBER_FOR = 24 * 3600.0
#: Seconds to wait before each automatic attempt; after the last, it's manual.
BACKOFF = (0.0, 120.0, 300.0, 900.0, 1800.0)
#: A busy CodeSpace (another command holds its SSH lock) is retried this soon.
BUSY_RETRY = 60.0
REPAIR_TIMEOUT = 900.0
LEASES_TIMEOUT = 60.0
BUSY_EXIT = 75

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{3,127}$")
_CODESPACE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{2,99}$")
_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$")
#: Fields the page may see.
_PUBLIC = ("session_id", "state", "codespace", "supervisor", "last_ok", "lost_at",
           "attempts", "next_try", "reconnecting", "last_error", "last_repair", "note")


def workers_file() -> Path:
    return effective_config_dir() / "ui-workers.json"


def _load(path: Path) -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def _save(path: Path, workers: dict[str, dict[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(workers, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.debug("could not save %s", path, exc_info=True)


def _venue(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("venue")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _supervisor(ref: Any) -> dict[str, str] | None:
    """``machine/project/worktree_id`` -> its parts, or ``None``."""
    parts = str(ref or "").split("/")
    if len(parts) != 3 or not all(parts) or not _OWNER.match(parts[2]):
        return None
    return {"machine": parts[0], "project": parts[1], "worktree_id": parts[2]}


def observe(workers: dict[str, dict[str, Any]], rows: list[dict[str, Any]], now: float) -> None:
    """Fold the bridge's live-session rows (including lapsed ones) into the
    remembered workers, classifying each as connected, lost, or stopped."""
    seen: set[str] = set()
    for row in rows:
        venue = _venue(row)
        sid = str(row.get("session_id") or "")
        if venue.get("kind") != "codespace" or not _ID.match(sid):
            continue
        codespace = str(venue.get("target") or "")
        if not _CODESPACE.match(codespace):
            continue
        seen.add(sid)
        w = workers.setdefault(sid, {"session_id": sid, "state": "connected", "first_seen": now})
        w.update(codespace=codespace, scope_id=str(row.get("worktree_id") or ""),
                 supervisor=venue.get("supervisor_ref") or w.get("supervisor"))
        fresh = row.get("status") == "live" and now - float(row.get("updated_at") or 0) <= STALE_AFTER
        if fresh:
            if w.get("state") != "connected":
                log.info("UI workers: %s on %s is connected", sid, codespace)
            w.update(state="connected", last_ok=now, lost_at=None, attempts=0, next_try=None,
                     last_error=None)
            continue
        if w.get("state") == "connected" and w.get("last_ok"):
            log.warning("UI workers: %s on %s lost its connection (row %s)", sid, codespace,
                        row.get("status"))
            w.update(state="lost", lost_at=now, attempts=0, next_try=now)
    for sid, w in workers.items():
        if sid in seen or w.get("state") != "connected":
            continue
        # Its row is gone. Removed while it was still heartbeating: deregistered
        # on purpose. Otherwise it lapsed while nobody was looking (the bridge
        # was down), and whether it was stopped is unknown: don't act on it.
        if now - float(w.get("last_ok") or 0) <= STALE_AFTER + 2 * SWEEP_INTERVAL:
            w.update(state="stopped", note="it was stopped or exited")
        else:
            w.update(state="lost", lost_at=now, attempts=len(BACKOFF), next_try=None,
                     note="it disconnected while the bridge wasn't watching")
    for sid in [s for s, w in workers.items()
                if now - float(w.get("last_ok") or w.get("first_seen") or 0) > REMEMBER_FOR]:
        workers.pop(sid, None)


def due(workers: dict[str, dict[str, Any]], now: float) -> list[dict[str, Any]]:
    return [w for w in workers.values()
            if w.get("state") == "lost" and not w.get("reconnecting")
            and w.get("next_try") is not None and now >= float(w["next_try"])]


def public(w: dict[str, Any]) -> dict[str, Any]:
    out = {k: w.get(k) for k in _PUBLIC}
    sup = _supervisor(w.get("supervisor"))
    out["supervisor"] = sup["worktree_id"] if sup else None
    return out


def _codespaces_cmd() -> list[str] | None:
    manifest = discover_provider_manifests().get("codespace")
    return list(manifest.command) if manifest else None


def _last_json(text: str) -> dict[str, Any]:
    """The last top-level JSON object in a command's stdout (progress precedes it)."""
    start = text.rfind("\n{")
    for idx in ([start + 1] if start >= 0 else []) + [0]:
        try:
            data = json.loads(text[idx:])
        except ValueError:
            continue
        if isinstance(data, dict):
            return data
    return {}


async def _run(argv: list[str], *, cwd: str | None, timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return -1, "", f"timed out after {int(timeout)}s"
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


class Workers:
    """The remembered workers and their reconnects, one per app."""

    def __init__(self, app: Any, path: Path | None = None) -> None:
        self.app = app
        self.path = path or workers_file()
        self.workers = _load(self.path)
        self.lock = asyncio.Lock()
        self.repairs: set[asyncio.Task] = set()

    async def sweep(self, *, act: bool = True) -> None:
        db = getattr(self.app.state, "db", None)
        if db is None:
            return
        rows = await asyncio.to_thread(db.list_live_sessions, include_dead=True)
        now = time.time()
        observe(self.workers, rows, now)
        if act:
            for w in due(self.workers, now):
                self.start(w)
        _save(self.path, self.workers)

    def start(self, w: dict[str, Any], *, manual: bool = False) -> None:
        w["reconnecting"] = True
        task = asyncio.create_task(self._reconnect(w, manual=manual), name=f"ui-worker-{w['session_id']}")
        self.repairs.add(task)
        task.add_done_callback(self.repairs.discard)

    async def _plan(self, w: dict[str, Any]) -> tuple[list[str] | None, str | None, str]:
        """(argv, cwd, why-not). The worker's own supervisor worktree runs it."""
        sup = _supervisor(w.get("supervisor"))
        if sup is None:
            return None, None, "it has no supervising task on this machine"
        st = _tasks.state_for(self.app)
        cache = st.get("cache") or await _tasks._refresh(st)
        row = next((r for r in cache.get("workspaces", []) if r.get("id") == sup["worktree_id"]), None)
        if row is None:
            return None, None, "its supervising task isn't on this machine"
        if row.get("machine") and str(row["machine"]).lower() != sup["machine"].lower():
            return None, None, f"its supervisor runs on {sup['machine']}"
        if row.get("status") != "active" or not row.get("path"):
            return None, None, "its supervising task isn't active"
        if not Path(row["path"]).is_dir():
            return None, None, "its supervising task's worktree is missing"
        cmd = _codespaces_cmd()
        if not cmd:
            return None, None, "agent-codespaces isn't installed on this machine"
        rc, out, err = await _run([*cmd, "leases", "--json"], cwd=row["path"], timeout=LEASES_TIMEOUT)
        try:
            leases = json.loads(out) if rc == 0 else None
        except ValueError:
            leases = None
        if not isinstance(leases, list):
            return None, None, "couldn't read the CodeSpace claims: " + (err.strip()[-200:] or f"exit {rc}")
        owner = next((str(x.get("owner") or "") for x in leases
                      if isinstance(x, dict) and x.get("codespace") == w["codespace"]), "")
        if not owner:
            return None, None, "its CodeSpace was released"
        if not _OWNER.match(owner):
            return None, None, "its CodeSpace claim has an unexpected owner"
        argv = [*cmd, "copilot", w["codespace"], "--detach", "--effort", owner,
                f"--copilot-arg=--resume={w['session_id']}"]
        return argv, row["path"], ""

    async def _reconnect(self, w: dict[str, Any], *, manual: bool) -> None:
        sid = w["session_id"]
        try:
            async with self.lock:
                argv, cwd, why = await self._plan(w)
                if argv is None:
                    self._settle(w, error=why, retry=False)
                    return
                log.info("UI workers: reconnecting %s on %s", sid, w["codespace"])
                w["last_repair"] = time.time()
                rc, out, err = await _run(argv, cwd=cwd, timeout=REPAIR_TIMEOUT)
            result = _last_json(out)
            if rc == 0 and result.get("ok"):
                got = result.get("session_id")
                note = None if got == sid else f"it came back as a new session ({str(got)[:8]})"
                w.update(state="connected" if got == sid else "stopped", last_ok=time.time(),
                         lost_at=None, attempts=0, next_try=None, last_error=None, note=note)
                log.info("UI workers: %s reconnected (%s)", sid, "resumed" if got == sid else "new session")
                return
            if rc == BUSY_EXIT:
                self._settle(w, error="another command is using the CodeSpace; retrying shortly",
                             retry=True, delay=BUSY_RETRY, count=False)
                return
            lines = [ln for ln in err.strip().splitlines() if ln.startswith("[FAIL]")] or err.strip().splitlines()
            detail = str(result.get("error") or "").strip() or (lines[-1] if lines else f"exit {rc}")
            self._settle(w, error=detail[:300], retry=not manual)
        except Exception as exc:  # noqa: BLE001 -- reported on the page
            log.warning("UI workers: reconnecting %s failed", sid, exc_info=True)
            self._settle(w, error=str(exc)[:300], retry=not manual)
        finally:
            w["reconnecting"] = False
            _save(self.path, self.workers)

    def _settle(self, w: dict[str, Any], *, error: str, retry: bool, delay: float | None = None,
                count: bool = True) -> None:
        attempts = int(w.get("attempts") or 0) + (1 if count else 0)
        nxt = None
        if retry and attempts < len(BACKOFF):
            nxt = time.time() + (BACKOFF[attempts] if delay is None else delay)
        w.update(attempts=attempts, next_try=nxt, last_error=error)


def workers_for(app: Any) -> Workers:
    ws = getattr(app.state, "ui_workers", None)
    if ws is None:
        ws = app.state.ui_workers = Workers(app)
    return ws


async def supervise(app: Any, *, is_active: Any = None, backoff: Any = None) -> None:
    """Background loop: keep the remembered workers current and reconnect lost
    ones. Acts only while ``is_active()`` (the routing table names this daemon)."""
    ws = workers_for(app)
    while True:
        await asyncio.sleep(SWEEP_INTERVAL)
        try:
            if backoff is not None and await backoff():
                continue
            act = True if is_active is None else bool(await asyncio.to_thread(is_active))
            await ws.sweep(act=act)
        except Exception:  # noqa: BLE001 -- never let the loop die
            log.warning("UI workers: sweep failed", exc_info=True)


@router.get("/api/v1/ui/workers", include_in_schema=False)
async def list_workers(request: Request) -> dict[str, Any]:
    ws = workers_for(request.app)
    await ws.sweep(act=False)
    return {"workers": [public(w) for w in ws.workers.values() if w.get("state") != "stopped"
                        or time.time() - float(w.get("last_ok") or 0) < 3600]}


@router.post("/api/v1/ui/workers/{session_id}/reconnect", include_in_schema=False)
async def reconnect_worker(session_id: str, request: Request) -> JSONResponse:
    if not _tasks._same_origin(request):
        return _tasks._refuse(403, "cross-origin requests may not reconnect workers")
    ws = workers_for(request.app)
    w = ws.workers.get(session_id)
    if w is None:
        return _tasks._refuse(404, "unknown worker")
    if w.get("reconnecting"):
        return JSONResponse(public(w))
    if w.get("state") == "connected":
        return _tasks._refuse(409, "that worker is connected")
    w.update(state="lost", note=None)
    ws.start(w, manual=True)
    return JSONResponse(public(w))

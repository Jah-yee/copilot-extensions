"""Tests for session and sign-in health on /ui (routes/ui_health.py)."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_bridge.routes import ui, ui_health

SID = "0bfeca19-5899-4556-970e-b9cd44718252"
SECRET = "s3cr3t-token-value"
SAME = {"Origin": "http://127.0.0.1:10756"}


def write_log(root, sid, events):
    d = root / sid
    d.mkdir(parents=True)
    (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def test_the_latest_uncleared_error_is_the_problem(tmp_path) -> None:
    write_log(tmp_path, SID, [
        {"type": "session.error", "data": {"errorType": "authorization", "statusCode": 401, "message": "401 Unauthorized"},
         "timestamp": "t1"},
    ])
    p = ui_health.latest_problem(SID, tmp_path)
    assert p["kind"] == "auth" and p["status_code"] == 401 and p["at"] == "t1"
    write_log(tmp_path, "other-session", [
        {"type": "session.error", "data": {"message": "boom"}},
        {"type": "session.resume", "data": {}},
    ])
    assert ui_health.latest_problem("other-session", tmp_path) is None
    assert ui_health.latest_problem("../escape", tmp_path) is None


class FakeStream:
    def __init__(self, text: str = "") -> None:
        self.lines = [ln.encode() for ln in text.splitlines(keepends=True)]

    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        return self.lines.pop(0) if self.lines else b""

    async def read(self) -> bytes:
        out = b"".join(self.lines)
        self.lines = []
        return out


class FakeStdin:
    def __init__(self, sink: list) -> None:
        self.sink = sink

    def write(self, data: bytes) -> None:
        self.sink.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeProc:
    def __init__(self, argv, script, stdin_sink) -> None:
        out, err, rc = script
        self.argv = argv
        self.stdout, self.stderr = FakeStream(out), FakeStream(err)
        self.stdin = FakeStdin(stdin_sink)
        self._rc = rc
        self.returncode = None

    async def wait(self) -> int:
        await asyncio.sleep(0)
        self.returncode = self._rc
        return self._rc

    async def communicate(self, data=None):
        if data:
            self.stdin.write(data)
        self.returncode = self._rc
        return await self.stdout.read(), await self.stderr.read()

    def kill(self) -> None:
        self.returncode = -9


@pytest.fixture
def cmds(monkeypatch):
    """Fake subprocesses: answer by the command's first word + verb."""
    calls: list = []
    stdin: list = []
    scripts = {
        "git credential fill": (f"protocol=https\nhost=ado.example\nusername=me\npassword={SECRET}\n", "", 0),
        "git credential approve": ("", "", 0),
        "az account get-access-token": ("", "", 0),
        "az login": ("", "", 0),
        "copilot login": ("To authenticate, visit https://github.com/login/device and enter code ABCD-1234\n", "", 0),
    }

    async def spawn(*argv, **kw):
        calls.append((list(argv), kw.get("env") or {}))
        key = " ".join(argv[:3]) if argv[0] != "copilot" else "copilot login"
        key = next(k for k in scripts if key.startswith(k) or " ".join(argv).startswith(k))
        return FakeProc(argv, scripts[key], stdin)

    monkeypatch.setattr(ui_health, "_spawn", spawn)
    monkeypatch.setattr(ui_health.shutil, "which", lambda name: name)

    async def profile(app):
        return {"sources": ["git-credential"], "ado_host": "ado.example",
                "azure_resources": [ui_health.ADO_RESOURCE, "https://storage.azure.com/"]}

    monkeypatch.setattr(ui_health, "relay_profile", profile)
    return SimpleNamespace(calls=calls, stdin=stdin, scripts=scripts)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ui.router)
    # One event loop for the whole test, so a sign-in's watcher keeps running.
    with TestClient(app, base_url="http://127.0.0.1:10756") as c:
        yield c


def test_host_auth_checks_the_relays_sign_ins_without_prompting_or_leaking(client, cmds) -> None:
    body = client.get("/api/v1/ui/host-auth").json()
    checks = {c["kind"]: c for c in body["checks"]}
    assert checks["ado"]["ok"] and checks["azure"]["ok"] and "ado.example" in checks["ado"]["label"]
    assert SECRET not in json.dumps(body)
    fill = next((argv, env) for argv, env in cmds.calls if argv[:3] == ["git", "credential", "fill"])
    assert fill[1]["GCM_INTERACTIVE"] == "never" and fill[1]["GIT_TERMINAL_PROMPT"] == "0"
    assert b"host=ado.example" in b"".join(cmds.stdin)
    az = next(argv for argv, _ in cmds.calls if argv[0] == "az")
    assert az[az.index("--resource") + 1] == ui_health.ADO_RESOURCE
    # Served from cache until it goes stale.
    n = len(cmds.calls)
    client.get("/api/v1/ui/host-auth")
    assert len(cmds.calls) == n


def test_a_failing_sign_in_is_reported_with_its_reason(client, cmds) -> None:
    cmds.scripts["git credential fill"] = ("", "fatal: interactive sign-in is required\n", 1)
    cmds.scripts["az account get-access-token"] = ("", "ERROR: AADSTS700082: The refresh token has expired\n", 1)
    checks = {c["kind"]: c for c in client.get("/api/v1/ui/host-auth").json()["checks"]}
    assert not checks["ado"]["ok"] and "interactive sign-in" in checks["ado"]["detail"]
    assert not checks["azure"]["ok"] and "AADSTS700082" in checks["azure"]["detail"]


def test_no_relay_means_nothing_to_check(client, cmds, monkeypatch) -> None:
    async def none(app):
        return None

    monkeypatch.setattr(ui_health, "relay_profile", none)
    assert client.get("/api/v1/ui/host-auth").json()["checks"] == []


def wait_done(client, kind):
    for _ in range(50):
        s = client.get(f"/api/v1/ui/sign-in/{kind}").json()
        if s["status"] != "waiting":
            return s
        time.sleep(0.02)
    raise AssertionError("sign-in never finished")


def test_copilot_sign_in_shows_its_device_code_then_completes(client, cmds) -> None:
    s = client.post("/api/v1/ui/sign-in/copilot", headers=SAME).json()
    assert s["status"] == "waiting" and s["code"] == "ABCD-1234" and s["url"] == "https://github.com/login/device"
    assert wait_done(client, "copilot")["status"] == "done"


def test_azure_devops_sign_in_runs_gcm_interactively_and_stores_the_credential(client, cmds) -> None:
    s = client.post("/api/v1/ui/sign-in/ado", headers=SAME).json()
    assert s["status"] == "waiting" and s["code"] is None
    done = wait_done(client, "ado")
    assert done["status"] == "done" and SECRET not in json.dumps(done)
    fill_env = next(env for argv, env in cmds.calls if argv[:3] == ["git", "credential", "fill"])
    # It may prompt now, even though agent shells (and so the daemon) say never.
    assert fill_env["GCM_INTERACTIVE"] == "auto"
    assert any(argv[:3] == ["git", "credential", "approve"] for argv, _ in cmds.calls)


def test_a_failed_azure_devops_sign_in_never_reports_gcm_output(client, cmds) -> None:
    cmds.scripts["git credential fill"] = (f"password={SECRET}\n", "fatal: user cancelled\n", 1)
    client.post("/api/v1/ui/sign-in/ado", headers=SAME)
    done = wait_done(client, "ado")
    assert done["status"] == "failed" and done["detail"] == "fatal: user cancelled"


def test_azure_sign_in_picks_up_a_device_code_if_offered(client, cmds) -> None:
    cmds.scripts["az login"] = ("", "To sign in, use a web browser to open the page "
                                "https://microsoft.com/devicelogin and enter the code F7GH2JK9L to authenticate.\n", 0)
    client.post("/api/v1/ui/sign-in/azure", headers=SAME)
    assert wait_done(client, "azure")["status"] == "done"


def test_sign_in_routes_refuse_unknown_providers_and_cross_origin(client, cmds) -> None:
    assert client.post("/api/v1/ui/sign-in/nope", headers=SAME).status_code == 404
    assert client.post("/api/v1/ui/sign-in/azure", headers={"Origin": "http://evil"}).status_code == 403
    assert client.delete("/api/v1/ui/sign-in/azure", headers={"Origin": "http://evil"}).status_code == 403
    assert client.get("/api/v1/ui/sign-in/azure").json()["status"] == "idle"


def test_the_restart_note_tells_the_session_what_it_lost() -> None:
    msg = ui_health.restart_message({"kind": "auth"}, "please continue the Playwright triage")
    assert "Copilot sign-in had failed" in msg and "watcher" in msg
    assert msg.endswith("The operator's message:\nplease continue the Playwright triage")
    assert "operator" not in ui_health.restart_message(None, "")


def live(sid, pid, *, registered=0.0, updated=0.0, status="live"):
    return SimpleNamespace(session_id=sid, pid=pid, registered_at=registered, updated_at=updated, status=status)


def test_a_resumed_session_is_recognised_by_its_new_process() -> None:
    started = 100.0
    assert ui_health._came_back(live(SID, 2, registered=50), SID, started, 1)  # same row, new pid
    assert not ui_health._came_back(live(SID, 1, registered=50, updated=200), SID, started, 1)
    assert ui_health._came_back(live(SID, None, updated=150), SID, started, None)
    assert not ui_health._came_back(live(SID, None, updated=90), SID, started, None)


def follow(monkeypatch, sequence, old_pid=1):
    app = FastAPI()
    request = SimpleNamespace(app=app)
    reads = iter(sequence)
    sent: list = []

    async def fake_live(req, worktree_id=None):
        return SimpleNamespace(live_sessions=next(reads))

    async def fake_post(sid, body, req):
        sent.append((sid, body))

    monkeypatch.setattr(ui_health._live, "list_live_sessions", fake_live)
    monkeypatch.setattr(ui_health._live, "post_live_message", fake_post)
    ui_health._restarts(request)["w1"] = outcome = {"state": "waiting"}
    asyncio.run(ui_health._follow_restart(request, "w1", SID, 100.0, old_pid, "the note", timeout=5, poll=0))
    return outcome, sent


def test_follow_restart_sends_the_note_once_the_session_is_back(monkeypatch) -> None:
    outcome, sent = follow(monkeypatch, [[live(SID, 1, registered=50)], [], [live(SID, 7, registered=50)]])
    assert outcome["state"] == "resumed" and outcome["note"] == "sent"
    assert len(sent) == 1 and sent[0][0] == SID and sent[0][1].body == "the note"
    assert sent[0][1].expected_session_id == SID and sent[0][1].idempotency_key.startswith("ui-restart:")


def test_follow_restart_reports_a_fresh_session(monkeypatch) -> None:
    outcome, sent = follow(monkeypatch, [[live("new-session-id", 9, registered=150)]])
    assert outcome["state"] == "fresh" and outcome["new_session_id"] == "new-session-id" and not sent

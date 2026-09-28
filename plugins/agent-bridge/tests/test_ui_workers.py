"""Tests for the /ui worker supervisor (routes/ui_workers.py): noticing a venue
worker that lost its connection and reconnecting it."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_bridge.routes import ui, ui_workers

SID = "bf8295a7-2ee7-4952-98e9-4c2cfadb3598"
CS = "ceo-list-sw-blank-x6pr995j4gwc6vxp"


def row(sid=SID, *, status="live", age=5.0, now=None, supervisor="box/harness/h1", kind="codespace"):
    now = time.time() if now is None else now
    return {"session_id": sid, "status": status, "updated_at": now - age, "worktree_id": f"anchor@{CS}",
            "venue": json.dumps({"kind": kind, "target": CS, "supervisor_ref": supervisor})}


def test_a_heartbeating_worker_is_connected_and_a_lapsed_one_is_lost() -> None:
    workers: dict = {}
    now = 1000.0
    ui_workers.observe(workers, [row(now=now)], now)
    assert workers[SID]["state"] == "connected" and workers[SID]["codespace"] == CS
    later = now + 400
    ui_workers.observe(workers, [row(now=later, age=300)], later)
    w = workers[SID]
    assert w["state"] == "lost" and w["next_try"] == later and w["attempts"] == 0
    assert ui_workers.due(workers, later) == [w]
    # Heartbeating again clears it.
    ui_workers.observe(workers, [row(now=later + 10)], later + 10)
    assert workers[SID]["state"] == "connected" and workers[SID]["next_try"] is None


def test_an_expired_row_is_lost_too() -> None:
    workers: dict = {}
    ui_workers.observe(workers, [row(now=1000.0)], 1000.0)
    ui_workers.observe(workers, [row(now=1060.0, status="expired", age=10)], 1060.0)
    assert workers[SID]["state"] == "lost"


def test_a_row_removed_while_heartbeating_was_stopped_on_purpose() -> None:
    workers: dict = {}
    ui_workers.observe(workers, [row(now=1000.0)], 1000.0)
    ui_workers.observe(workers, [], 1045.0)  # --stop deregisters it
    assert workers[SID]["state"] == "stopped"
    assert ui_workers.due(workers, 5000.0) == []


def test_a_row_that_vanished_unobserved_is_lost_but_not_retried_automatically() -> None:
    workers: dict = {}
    ui_workers.observe(workers, [row(now=1000.0)], 1000.0)
    ui_workers.observe(workers, [], 1000.0 + 3 * 3600)  # the bridge wasn't watching
    w = workers[SID]
    assert w["state"] == "lost" and w["next_try"] is None
    assert ui_workers.due(workers, 1000.0 + 4 * 3600) == []


def test_local_sessions_and_malformed_rows_are_ignored_and_old_workers_forgotten() -> None:
    workers: dict = {}
    now = 1000.0
    ui_workers.observe(workers, [row(kind="container"), row("bad id!"),
                                 {**row(), "venue": json.dumps({"kind": "codespace", "target": "x y"})},
                                 {"session_id": "local-session", "status": "live", "updated_at": now}], now)
    assert workers == {}
    ui_workers.observe(workers, [row(now=now)], now)
    ui_workers.observe(workers, [], now + ui_workers.REMEMBER_FOR + 1)
    assert workers == {}


class FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def list_live_sessions(self, worktree_id=None, include_dead=False):
        assert include_dead
        return list(self.rows)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """A worker that lost its connection, its supervising worktree, and a fake agent-codespaces."""
    supervisor = tmp_path / "h1"
    supervisor.mkdir()
    calls: list = []
    answers = {"leases": (0, json.dumps([{"codespace": CS, "owner": "ceo-list", "kind": "claim"}]), ""),
               "copilot": (0, "progress\n" + json.dumps({"ok": True, "session_id": SID}, indent=2) + "\n", "")}

    async def fake_run(argv, *, cwd, timeout):
        calls.append((argv, cwd))
        return answers[argv[1]]

    monkeypatch.setattr(ui_workers, "_run", fake_run)
    monkeypatch.setattr(ui_workers, "_codespaces_cmd", lambda: ["agent-codespaces"])
    app = FastAPI()
    app.include_router(ui.router)
    app.state.db = FakeDb([])
    app.state.ui_tasks = {"cache": {"workspaces": [
        {"id": "h1", "status": "active", "path": str(supervisor), "machine": "box", "project": "harness"},
        {"id": "h2", "status": "finalized", "path": str(supervisor), "project": "harness"},
    ]}, "refresh": None, "launches": [], "lock": asyncio.Lock(), "prs": {}, "subjects": {}}
    ws = ui_workers.Workers(app, path=tmp_path / "ui-workers.json")
    app.state.ui_workers = ws
    ws.workers[SID] = {"session_id": SID, "state": "lost", "codespace": CS, "supervisor": "box/harness/h1",
                       "last_ok": time.time() - 600, "attempts": 0, "next_try": time.time()}
    return SimpleNamespace(app=app, ws=ws, calls=calls, answers=answers, supervisor=supervisor)


def reconnect(env, **kw):
    asyncio.run(env.ws._reconnect(env.ws.workers[SID], **kw))
    return env.ws.workers[SID]


def test_reconnect_resumes_the_same_session_from_its_supervisors_worktree(env) -> None:
    w = reconnect(env, manual=False)
    assert env.calls, w.get("last_error")
    argv, cwd = env.calls[-1]
    assert argv == ["agent-codespaces", "copilot", CS, "--detach", "--effort", "ceo-list",
                    f"--copilot-arg=--resume={SID}"]
    assert cwd == str(env.supervisor)
    assert w["state"] == "connected" and w["reconnecting"] is False and w["last_error"] is None
    assert json.loads((env.ws.path).read_text())[SID]["state"] == "connected"


def test_a_busy_codespace_is_retried_soon_without_using_an_attempt(env) -> None:
    env.answers["copilot"] = (ui_workers.BUSY_EXIT, "", "[BUSY] An SSH operation is already in progress")
    w = reconnect(env, manual=False)
    assert w["state"] == "lost" and w["attempts"] == 0
    assert w["next_try"] == pytest.approx(time.time() + ui_workers.BUSY_RETRY, abs=5)


def test_a_failed_reconnect_backs_off_then_gives_up(env) -> None:
    env.answers["copilot"] = (1, json.dumps({"ok": False, "error": "the Connection Owner could not be started"}), "")
    for attempt in range(1, len(ui_workers.BACKOFF) + 1):
        w = reconnect(env, manual=False)
        assert w["attempts"] == attempt and w["last_error"] == "the Connection Owner could not be started"
    assert w["next_try"] is None and w["state"] == "lost"


@pytest.mark.parametrize(("change", "why"), [
    (lambda e: e.answers.__setitem__("leases", (0, "[]", "")), "its CodeSpace was released"),
    (lambda e: e.ws.workers[SID].__setitem__("supervisor", "box/harness/h2"), "its supervising task isn't active"),
    (lambda e: e.ws.workers[SID].__setitem__("supervisor", "other/harness/h1"), "its supervisor runs on other"),
    (lambda e: e.ws.workers[SID].__setitem__("supervisor", None), "it has no supervising task on this machine"),
])
def test_a_worker_is_not_reconnected_without_a_live_claim_and_an_active_supervisor(env, change, why) -> None:
    change(env)
    w = reconnect(env, manual=False)
    assert w["last_error"] == why and w["next_try"] is None
    assert not any(argv[1] == "copilot" for argv, _ in env.calls)


def test_a_new_session_in_its_place_retires_the_old_worker(env) -> None:
    env.answers["copilot"] = (0, json.dumps({"ok": True, "session_id": "c9045aa5-2646-4f1b-83ad-6122526c9131"}), "")
    w = reconnect(env, manual=False)
    assert w["state"] == "stopped" and "new session" in w["note"]


def test_routes_list_and_reconnect_workers(env) -> None:
    client = TestClient(env.app, base_url="http://127.0.0.1:10756")
    listed = client.get("/api/v1/ui/workers").json()["workers"]
    assert [w["session_id"] for w in listed] == [SID]
    assert listed[0]["supervisor"] == "h1" and "scope_id" not in listed[0]
    same = {"Origin": "http://127.0.0.1:10756"}
    assert client.post(f"/api/v1/ui/workers/{SID}/reconnect", headers={"Origin": "http://evil"}).status_code == 403
    assert client.post("/api/v1/ui/workers/nope-nope/reconnect", headers=same).status_code == 404
    started: list = []
    env.ws.start = lambda w, manual=False: started.append((w["session_id"], manual))
    assert client.post(f"/api/v1/ui/workers/{SID}/reconnect", headers=same).status_code == 200
    assert started == [(SID, True)]
    env.ws.workers[SID]["state"] = "connected"
    env.ws.workers[SID]["reconnecting"] = False
    assert client.post(f"/api/v1/ui/workers/{SID}/reconnect", headers=same).status_code == 409


def test_the_supervisor_loop_only_acts_while_this_daemon_is_active(env, monkeypatch) -> None:
    sweeps: list = []

    async def fake_sweep(*, act=True):
        sweeps.append(act)
        if len(sweeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(ui_workers, "SWEEP_INTERVAL", 0)
    env.ws.sweep = fake_sweep
    active = iter([False, True])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(ui_workers.supervise(env.app, is_active=lambda: next(active)))
    assert sweeps == [False, True]

"""Tests for Phase 5 live-session representation (translate + store + routes)."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_bridge.db import Database
from agent_bridge.events import EventLog
from agent_bridge.live_representation import (
    LiveEventStore,
    await_turn_reply,
    translate_sdk_event,
)
from agent_bridge.routes import live_sessions


# -- Translation ------------------------------------------------------------


class TestTranslateSdkEvent:
    def test_user_and_assistant_message(self) -> None:
        assert translate_sdk_event("user.message", {"content": "hi"}) == [
            ("user_message", {"content": "hi"})
        ]
        assert translate_sdk_event("assistant.message", {"content": "yo"}) == [
            ("agent_message", {"text": "yo"})
        ]

    def test_a_bridge_delivered_message_carries_its_text(self) -> None:
        envelope = (
            "<current_datetime>x</current_datetime>\n\n"
            # As renderDeliveredPrompt emits a plain prompt: no kind attribute.
            '<agent-message from="D:\\repos\\h.worktrees\\w1" reply-to="w1" msg-id="156">\n'
            "Operator: please re-check the Playwright run.\n</agent-message>"
        )
        out = translate_sdk_event("user.message", {
            "content": "Message from D:\\repos\\h.worktrees\\w1 (via agent-bridge)",
            "source": "agent-bridge", "transformedContent": envelope,
        })
        assert out == [("user_message", {
            "content": "Message from D:\\repos\\h.worktrees\\w1 (via agent-bridge)",
            "relay_body": "Operator: please re-check the Playwright run.",
            "relay_from": "D:\\repos\\h.worktrees\\w1", "relay_kind": "prompt",
        })]
        # Only the bridge's own deliveries are read; anything else is unchanged.
        assert translate_sdk_event("user.message", {
            "content": "hi", "transformedContent": "<agent-message>x</agent-message>",
        }) == [("user_message", {"content": "hi"})]
        long = translate_sdk_event("user.message", {
            "content": "h", "source": "agent-bridge",
            "transformedContent": "<agent-message>" + "y" * 9000 + "</agent-message>",
        })[0][1]["relay_body"]
        assert len(long) == 8001 and long.endswith("\u2026")

    def test_a_relayed_message_is_read_whole_with_its_sender_unescaped(self) -> None:
        def relay(envelope: str) -> dict:
            return translate_sdk_event("user.message", {
                "content": "h", "source": "agent-bridge", "transformedContent": envelope})[0][1]

        # The body is literal, so it may mention the closing tag itself.
        out = relay('<agent-message from="a&amp;b &quot;q&quot; &lt;x&gt;" kind="notify">\n'
                    "Close it with </agent-message> as usual.\n\n(notify guidance)\n</agent-message>")
        assert out["relay_body"] == "Close it with </agent-message> as usual.\n\n(notify guidance)"
        assert out["relay_from"] == 'a&b "q" <x>' and out["relay_kind"] == "notify"

    def test_reasoning_maps_to_thought(self) -> None:
        assert translate_sdk_event(
            "assistant.reasoning", {"content": "thinking"}
        ) == [("agent_thought", {"text": "thinking"})]

    def test_empty_text_is_dropped(self) -> None:
        assert translate_sdk_event("assistant.message", {"content": ""}) == []
        assert translate_sdk_event("assistant.message", {}) == []

    def test_tool_start(self) -> None:
        out = translate_sdk_event(
            "tool.execution_start",
            {"toolCallId": "t1", "toolName": "bash", "arguments": {"cmd": "ls"}},
        )
        assert out == [(
            "tool_call_start",
            {
                "tool_call_id": "t1",
                "title": "bash",
                "kind": "bash",
                "raw_input": {"cmd": "ls"},
            },
        )]

    def test_tool_start_without_id_dropped(self) -> None:
        assert translate_sdk_event("tool.execution_start", {"toolName": "x"}) == []

    def test_tool_complete_success(self) -> None:
        out = translate_sdk_event(
            "tool.execution_complete",
            {
                "toolCallId": "t1",
                "success": True,
                "result": {"content": "short", "detailedContent": "full diff"},
            },
        )
        assert out == [(
            "tool_call_update",
            {
                "tool_call_id": "t1",
                "status": "completed",
                "content": ["full diff"],
                "raw_output": None,
            },
        )]

    def test_tool_complete_failure_carries_error(self) -> None:
        out = translate_sdk_event(
            "tool.execution_complete",
            {
                "toolCallId": "t1",
                "success": False,
                "error": {"message": "boom"},
            },
        )
        assert out[0][0] == "tool_call_update"
        data = out[0][1]
        assert data["status"] == "failed"
        assert "boom" in data["content"]

    def test_usage_and_context(self) -> None:
        assert translate_sdk_event(
            "assistant.usage",
            {"inputTokens": 10, "outputTokens": 5, "model": "gpt"},
        ) == [(
            "usage_update",
            {
                "input_tokens": 10,
                "output_tokens": 5,
                "model": "gpt",
                "context_size": None,
                "context_used": None,
            },
        )]
        assert translate_sdk_event(
            "session.usage_info", {"currentTokens": 100, "tokenLimit": 2000}
        ) == [(
            "usage_update",
            {
                "input_tokens": None,
                "output_tokens": None,
                "model": None,
                "context_size": 2000,
                "context_used": 100,
            },
        )]

    def test_turn_end(self) -> None:
        assert translate_sdk_event("assistant.turn_end", {"turnId": "x"}) == [
            ("turn_complete", {"stop_reason": None})
        ]

    def test_compaction_events(self) -> None:
        # Example Labs #7587: compaction was dropped before this whitelist
        # entry existed, so a downstream ctx% reset showed with no
        # confirmation a compaction actually happened.
        assert translate_sdk_event(
            "session.compaction_start",
            {"conversationTokens": 1000, "systemTokens": 200},
        ) == [(
            "compaction_start",
            {"conversation_tokens": 1000, "system_tokens": 200},
        )]
        assert translate_sdk_event(
            "session.compaction_complete",
            {"success": True, "tokensRemoved": 800, "postCompactionTokens": 400},
        ) == [(
            "compaction_complete",
            {
                "success": True,
                "tokens_removed": 800,
                "post_compaction_tokens": 400,
            },
        )]

    def test_permission_is_read_only_without_request_id(self) -> None:
        out = translate_sdk_event(
            "permission.requested",
            {
                "requestId": "req-42",
                "permissionRequest": {
                    "kind": "shell",
                    "intention": "list files",
                    "fullCommandText": "ls -la",
                },
            },
        )
        assert len(out) == 1
        event_type, data = out[0]
        assert event_type == "permission_request"
        assert data["read_only"] is True
        assert data["kind"] == "shell"
        assert data["intention"] == "list files"
        # The two-writer safety line: NEVER carry a correlation id a remote
        # viewer could use to answer the prompt.
        assert "requestId" not in data
        assert "request_id" not in data
        assert "req-42" not in str(data)

    def test_ask_user_tool_start_is_read_only_request(self) -> None:
        """A live CLI session's ask_user surfaces as a legible, READ-ONLY
        ask_user_request (take-over to answer) -- never an opaque spinner."""
        out = translate_sdk_event(
            "tool.execution_start",
            {
                "toolCallId": "tc-ask",
                "toolName": "ask_user",
                "arguments": {
                    "message": "Pick one",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"choice": {"type": "string"}},
                    },
                },
            },
        )
        assert len(out) == 1
        event_type, data = out[0]
        assert event_type == "ask_user_request"
        assert data["tool_call_id"] == "tc-ask"
        assert data["message"] == "Pick one"
        assert data["requested_schema"]["properties"] == {
            "choice": {"type": "string"}
        }
        # Represented CLI session: owner Copilot holds the reply, so this is
        # read-only downstream (answer affordance is take-over, not inline).
        assert data["read_only"] is True

    def test_agent_id_passed_through(self) -> None:
        out = translate_sdk_event(
            "assistant.message", {"content": "sub", "agentId": "sub-1"}
        )
        assert out == [("agent_message", {"text": "sub", "agent_id": "sub-1"})]

    def test_unknown_type_dropped(self) -> None:
        assert translate_sdk_event("assistant.streaming_delta", {"x": 1}) == []
        assert translate_sdk_event("session.plan_changed", {}) == []
        assert translate_sdk_event("nonsense", {}) == []


# -- LiveEventStore ---------------------------------------------------------


class TestLiveEventStore:
    def test_get_or_create_is_stable(self) -> None:
        store = LiveEventStore()
        assert store.get("s") is None
        log = store.get_or_create("s")
        assert store.get("s") is log
        assert store.get_or_create("s") is log

    def test_ingest_translates_and_appends(self) -> None:
        store = LiveEventStore()
        n = store.ingest(
            "s",
            [
                {"type": "user.message", "data": {"content": "hi"}},
                {"type": "assistant.message", "data": {"content": "yo"}},
                {"type": "assistant.streaming_delta", "data": {"x": 1}},  # dropped
            ],
        )
        assert n == 2
        log = store.get("s")
        assert log is not None
        events = log.get_events()
        assert [e.event for e in events] == ["user_message", "agent_message"]
        assert log.latest_id == 2

    def test_ingest_skips_malformed_items(self) -> None:
        store = LiveEventStore()
        n = store.ingest(
            "s",
            ["not a dict", {"no_type": 1}, {"type": 5}, {"type": "user.message"}],
        )
        # only the last (type=user.message, empty data -> no content) contributes
        # nothing; all are safely skipped without error.
        assert n == 0

    def test_ingest_dedups_by_event_id(self) -> None:
        # The CLI runtime redelivers each session.event once per live-session
        # subscription -- N copies sharing one id. ingest must append only once.
        store = LiveEventStore()
        event = {"type": "user.message", "id": "evt-1", "data": {"content": "hi"}}
        n = store.ingest("s", [event, dict(event), dict(event), dict(event), dict(event)])
        assert n == 1
        log = store.get("s")
        assert log is not None
        assert [e.event for e in log.get_events()] == ["user_message"]
        # A later batch replaying the same id (e.g. a retried POST) adds nothing.
        assert store.ingest("s", [dict(event)]) == 0
        # A genuinely new id still appends.
        assert store.ingest(
            "s", [{"type": "assistant.message", "id": "evt-2", "data": {"content": "yo"}}]
        ) == 1

    def test_ingest_without_id_appends_each(self) -> None:
        # No id -> can't dedup; preserve the prior best-effort behavior.
        store = LiveEventStore()
        item = {"type": "user.message", "data": {"content": "hi"}}
        n = store.ingest("s", [dict(item), dict(item)])
        assert n == 2

    def test_ingest_dedup_is_per_session(self) -> None:
        store = LiveEventStore()
        event = {"type": "user.message", "id": "evt-1", "data": {"content": "hi"}}
        assert store.ingest("a", [dict(event)]) == 1
        # Same id under a different session is independent -> appended.
        assert store.ingest("b", [dict(event)]) == 1

    def test_drop_resets_dedup(self) -> None:
        store = LiveEventStore()
        event = {"type": "user.message", "id": "evt-1", "data": {"content": "hi"}}
        assert store.ingest("s", [dict(event)]) == 1
        store.drop("s")
        # After drop the session's dedup memory is gone, so the id ingests again.
        assert store.ingest("s", [dict(event)]) == 1

    def test_drop_forgets_log(self) -> None:
        store = LiveEventStore()
        store.ingest("s", [{"type": "assistant.message", "data": {"content": "a"}}])
        assert store.get("s") is not None
        store.drop("s")
        assert store.get("s") is None
        # dropping an unknown id is a no-op
        store.drop("nope")


# -- Routes -----------------------------------------------------------------


@pytest.fixture
def client(tmp_db: Database) -> TestClient:
    app = FastAPI()
    app.state.db = tmp_db
    app.state.live_event_store = LiveEventStore()
    app.include_router(live_sessions.router)
    return TestClient(app)


def _register(client: TestClient, sid: str = "cli-1") -> None:
    r = client.post("/api/v1/live-sessions", json={"session_id": sid})
    assert r.status_code == 200, r.text


def test_ingest_requires_registration(client: TestClient) -> None:
    r = client.post(
        "/api/v1/live-sessions/ghost/events",
        json={"events": [{"type": "assistant.message", "data": {"content": "x"}}]},
    )
    assert r.status_code == 404


def test_ingest_translates_and_counts(client: TestClient) -> None:
    _register(client)
    r = client.post(
        "/api/v1/live-sessions/cli-1/events",
        json={
            "events": [
                {"type": "user.message", "data": {"content": "hello"}},
                {"type": "assistant.message", "data": {"content": "hi there"}},
                {"type": "assistant.usage", "data": {"model": "gpt"}},
            ]
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["session_id"] == "cli-1"
    assert body["ingested"] == 3
    assert body["last_id"] == 3


def test_open_tool_keeps_live_session_running_after_turn_end(
    client: TestClient,
) -> None:
    _register(client)
    r = client.post(
        "/api/v1/live-sessions/cli-1/events",
        json={
            "events": [
                {
                    "type": "tool.execution_start",
                    "data": {
                        "toolCallId": "tc-1",
                        "toolName": "shell",
                        "arguments": {"command": "run checks"},
                    },
                },
                {"type": "assistant.turn_end", "data": {"turnId": "subturn"}},
            ]
        },
    )
    assert r.status_code == 200, r.text

    info = client.get("/api/v1/live-sessions/cli-1").json()
    assert info["turn_state"] == "running"
    assert info["liveness"] == "active"

    snapshot = client.get("/api/v1/live-sessions/cli-1/result").json()
    assert snapshot["state"]["session_status"] == "live"
    assert snapshot["state"]["liveness"] == "active"
    assert snapshot["state"]["active_work"]["availability"] == "available"
    assert snapshot["state"]["active_work"]["value"]["tool"]["command"] == "run checks"


def test_deregister_drops_represented_log(client: TestClient) -> None:
    _register(client)
    client.post(
        "/api/v1/live-sessions/cli-1/events",
        json={"events": [{"type": "assistant.message", "data": {"content": "a"}}]},
    )
    store: LiveEventStore = client.app.state.live_event_store
    assert store.get("cli-1") is not None
    assert client.delete("/api/v1/live-sessions/cli-1").json()["ok"] is True
    assert store.get("cli-1") is None


def test_stream_requires_registration(client: TestClient) -> None:
    r = client.get("/api/v1/live-sessions/ghost/events")
    assert r.status_code == 404


def test_stream_replays_represented_tail() -> None:
    """The represented SSE reuses the ACP ``_sse_event_stream`` helper.

    Driven directly (rather than over TestClient's infinite stream) so the tail
    replay is deterministic: break out once both buffered events are seen, which
    ``aclose()``s the generator before its first 30s quiet-period wait.
    """
    import asyncio

    from agent_bridge.routes.live_sessions import _RepresentedSession
    from agent_bridge.routes.sessions import _sse_event_stream

    store = LiveEventStore()
    store.ingest(
        "cli-1",
        [
            {"type": "user.message", "data": {"content": "q"}},
            {"type": "assistant.message", "data": {"content": "a"}},
        ],
    )
    shim = _RepresentedSession(session_id="cli-1", event_log=store.get("cli-1"))

    async def _run() -> str:
        collected: list[str] = []
        gen = _sse_event_stream(
            shim, 0, server=None, is_disconnected=None, mgr=None
        )
        seen = 0
        async for chunk in gen:
            collected.append(chunk)
            if chunk.startswith("id:"):
                seen += 1
            if seen >= 2:
                break
        await gen.aclose()
        return "".join(collected)

    blob = asyncio.run(_run())
    assert "user_message" in blob
    assert "agent_message" in blob



# -- D1: read a live session's reply turn (await_turn_reply + wait route) ----


class TestAwaitTurnReply:
    @pytest.mark.asyncio
    async def test_collects_agent_text_until_turn_complete(self) -> None:
        log = EventLog()
        log.append("user_message", {"content": "injected"})
        log.append("agent_message", {"text": "part one "})
        log.append("agent_message", {"text": "part two"})
        log.append("turn_complete", {"stop_reason": "end_turn"})
        reply = await await_turn_reply(log, after=0, timeout=1.0)
        assert reply["replied"] is True
        assert reply["reply"] == "part one part two"
        assert reply["stop_reason"] == "end_turn"

    @pytest.mark.asyncio
    async def test_ignores_events_at_or_before_after(self) -> None:
        # A turn that completed *before* the send must not be read as the reply.
        log = EventLog()
        log.append("agent_message", {"text": "old"})
        head = log.latest_id
        log.append("turn_complete", {"stop_reason": "old"})
        # after=head+1 skips the already-present turn_complete; nothing new -> timeout
        reply = await await_turn_reply(log, after=head + 1, timeout=0.05)
        assert reply["replied"] is False
        assert reply["reply"] is None

    @pytest.mark.asyncio
    async def test_times_out_with_no_turn(self) -> None:
        log = EventLog()
        log.append("agent_message", {"text": "partial only"})
        reply = await await_turn_reply(log, after=0, timeout=0.05)
        assert reply["replied"] is False
        # partial assistant text is still returned on timeout
        assert reply["reply"] == "partial only"


def test_route_wait_times_out_but_queues(client: TestClient) -> None:
    # A wait with no reply forthcoming returns replied=False, and the message
    # still sits in the durable queue for later delivery.
    _register(client, "cli-wait")
    r = client.post(
        "/api/v1/live-sessions/cli-wait/messages",
        json={"sender": "peer", "body": "status?", "wait": True,
              "wait_timeout": 0.05},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replied"] is False
    assert body["reply"] is None
    assert body["message_id"] > 0
    pending = client.get("/api/v1/live-sessions/cli-wait/messages").json()
    assert [m["body"] for m in pending["messages"]] == ["status?"]


def test_route_no_wait_returns_immediately(client: TestClient) -> None:
    _register(client, "cli-nw")
    r = client.post(
        "/api/v1/live-sessions/cli-nw/messages",
        json={"sender": "peer", "body": "fyi"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["replied"] is False
    assert body["message_id"] > 0



def test_a_merge_keeps_successor_waiters_and_tokens_valid() -> None:
    import asyncio

    from agent_bridge.live_representation import LiveEventStore, await_turn_reply
    from agent_bridge.result_tokens import _decode_token, _position_token, retarget

    store = LiveEventStore()

    async def scenario():
        pred = store.get_or_create("placeholder")
        pred.append("agent_message", {"text": "before"})
        succ = store.get_or_create("resumed")  # registered before its PID resolved
        succ.append("agent_message", {"text": "early"})
        token = _position_token("represented", "resumed", succ.continuity_id, succ.latest_id)
        waiting = asyncio.create_task(await_turn_reply(succ, after=succ.latest_id, timeout=5))
        await asyncio.sleep(0.05)
        store.alias("placeholder", "resumed")
        pred.append("agent_message", {"text": "the reply"})
        pred.append("turn_complete", {"stop_reason": "end_turn"})
        reply = await asyncio.wait_for(waiting, 5)
        moved = retarget(token, store.merged_history())
        decoded = _decode_token(moved, source="represented", session_id="resumed",
                                kinds=frozenset({"position"}))
        return reply, decoded, pred

    reply, decoded, pred = asyncio.run(scenario())
    assert reply["replied"] and reply["reply"] == "the reply"
    assert decoded["continuity"] == pred.continuity_id
    assert [e.data.get("text") for e in pred.get_events(0)][decoded["event_id"] - 1] == "early"


def test_retarget_leaves_a_malformed_continuity_for_validation() -> None:
    from agent_bridge.result_tokens import _encode_token, retarget

    merged = {"old-log": ("new-log", {1: 3})}
    for bad in ([], {}, 7):
        token = _encode_token({"kind": "position", "continuity": bad, "event_id": 1})
        assert retarget(token, merged) == token  # no TypeError -> the route's 4xx path


def test_retarget_never_launders_an_oversized_or_non_base64_token() -> None:
    import base64
    import json

    from agent_bridge.result_tokens import _MAX_DETAIL_TOKEN_CHARS, _TOKEN_PREFIX, retarget

    merged = {"old-log": ("new-log", {1: 3})}
    payload = {"v": 1, "kind": "position", "source": "represented", "session_id": "s",
               "continuity": "old-log", "event_id": 1}
    padded = json.dumps(payload) + " " * _MAX_DETAIL_TOKEN_CHARS
    oversized = _TOKEN_PREFIX + base64.urlsafe_b64encode(padded.encode()).decode().rstrip("=")
    assert len(oversized) > _MAX_DETAIL_TOKEN_CHARS
    assert retarget(oversized, merged) == oversized
    raw = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    junk = _TOKEN_PREFIX + raw[:8] + "!!!!" + raw[8:]  # lenient decoding would drop these
    assert retarget(junk, merged) == junk


def test_retarget_leaves_unsupported_token_versions_for_validation() -> None:
    import base64
    import json

    import pytest

    from agent_bridge.result_tokens import ResultTokenError, _decode_token, _TOKEN_PREFIX, retarget

    merged = {"old-log": ("new-log", {1: 3})}
    for version in (2, None):
        payload = {"kind": "position", "source": "represented", "session_id": "s",
                   "continuity": "old-log", "event_id": 1}
        if version is not None:
            payload["v"] = version
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        token = _TOKEN_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        assert retarget(token, merged) == token
        with pytest.raises(ResultTokenError):
            _decode_token(retarget(token, merged), source="represented", session_id="s",
                          kinds=frozenset({"position"}))


def test_a_waited_send_on_an_empty_successor_skips_the_predecessors_old_turn() -> None:
    import asyncio

    from agent_bridge.live_representation import LiveEventStore, await_turn_reply

    store = LiveEventStore()

    async def scenario():
        pred = store.get_or_create("placeholder")
        pred.append("agent_message", {"text": "old answer"})
        pred.append("turn_complete", {"stop_reason": "end_turn"})
        succ = store.get_or_create("resumed")  # empty log, cursor 0
        waiting = asyncio.create_task(await_turn_reply(succ, after=succ.latest_id, timeout=5))
        await asyncio.sleep(0.05)
        store.alias("placeholder", "resumed")
        pred.append("agent_message", {"text": "new reply"})
        pred.append("turn_complete", {"stop_reason": "end_turn"})
        return await asyncio.wait_for(waiting, 5)

    reply = asyncio.run(scenario())
    assert reply["replied"] and reply["reply"] == "new reply"


def test_merge_mappings_outlive_an_alias_but_not_the_last_key() -> None:
    from agent_bridge.live_representation import LiveEventStore

    store = LiveEventStore()
    store.get_or_create("placeholder").append("agent_message", {"text": "a"})
    store.get_or_create("resumed").append("agent_message", {"text": "b"})
    store.alias("placeholder", "resumed")
    assert store.merged_history()
    store.drop("placeholder")  # "resumed" still serves the merged log
    assert store.merged_history()
    store.drop("resumed")  # canonical cleanup: nothing serves it any more
    assert store.merged_history() == {}


def test_an_open_successor_stream_follows_a_merge_without_replaying() -> None:
    import asyncio

    from agent_bridge.live_representation import LiveEventStore
    from agent_bridge.routes.live_sessions import _RepresentedSession
    from agent_bridge.routes.sessions import _sse_event_stream

    store = LiveEventStore()

    async def scenario():
        pred = store.get_or_create("placeholder")
        pred.append("agent_message", {"text": "p1"})
        pred.append("agent_message", {"text": "p2"})
        succ = store.get_or_create("resumed")
        succ.append("agent_message", {"text": "s1"})  # already consumed: cursor 1
        shim = _RepresentedSession("resumed", succ, store)
        stream = _sse_event_stream(shim, 1, server=None, is_disconnected=None,
                                   heartbeat_interval=0.2)
        store.alias("placeholder", "resumed")  # s1 becomes id 3 after p1, p2
        pred.append("agent_message", {"text": "after"})
        chunks = [str(await asyncio.wait_for(stream.__anext__(), 5))]
        await stream.aclose()
        return chunks

    chunks = asyncio.run(scenario())
    joined = "".join(chunks)
    assert "after" in joined
    assert "p1" not in joined and "p2" not in joined and "s1" not in joined


def _route_request(store, session_id: str):
    from types import SimpleNamespace

    db = SimpleNamespace(get_live_session=lambda _sid: {"session_id": session_id, "worktree_id": None})
    state = SimpleNamespace(db=db, live_event_store=store, uvicorn_server=None)
    return SimpleNamespace(app=SimpleNamespace(state=state), is_disconnected=None)


def test_a_reconnect_across_a_merge_resumes_after_the_consumed_events() -> None:
    """Disconnect before the merge, reconnect after it: the viewer's cursor is
    numbered on the discarded successor log and is translated, not replayed."""
    import asyncio

    from agent_bridge.live_representation import LiveEventStore
    from agent_bridge.routes.live_sessions import stream_live_events

    store = LiveEventStore()

    async def scenario():
        pred = store.get_or_create("placeholder")
        pred.append("agent_message", {"text": "p1"})
        pred.append("agent_message", {"text": "p2"})
        succ = store.get_or_create("resumed")
        succ.append("agent_message", {"text": "s1"})
        first = await stream_live_events("resumed", _route_request(store, "resumed"), after=1)
        seen_continuity = first.headers["X-Agent-Bridge-Continuity"]
        assert seen_continuity == succ.continuity_id
        store.alias("placeholder", "resumed")  # s1 becomes merged id 3
        pred.append("agent_message", {"text": "after"})
        again = await stream_live_events(
            "resumed", _route_request(store, "resumed"), after=1, continuity_id=seen_continuity)
        assert again.headers["X-Agent-Bridge-Continuity"] == pred.continuity_id
        body = again.body_iterator
        chunk = str(await asyncio.wait_for(body.__anext__(), 5))
        await body.aclose()
        return chunk

    chunk = asyncio.run(scenario())
    assert "after" in chunk and chunk.startswith("id: 4")
    assert "p1" not in chunk and "p2" not in chunk and "s1" not in chunk


def test_an_open_stream_announces_the_merged_continuity_before_renumbered_ids() -> None:
    import asyncio

    from agent_bridge.live_representation import LiveEventStore
    from agent_bridge.routes.live_sessions import stream_live_events

    store = LiveEventStore()

    async def scenario():
        pred = store.get_or_create("placeholder")
        pred.append("agent_message", {"text": "p1"})
        succ = store.get_or_create("resumed")
        succ.append("agent_message", {"text": "s1"})
        response = await stream_live_events("resumed", _route_request(store, "resumed"), after=1)
        body = response.body_iterator
        store.alias("placeholder", "resumed")
        pred.append("agent_message", {"text": "after"})
        chunks = [str(await asyncio.wait_for(body.__anext__(), 5)) for _ in range(2)]
        await body.aclose()
        return chunks, pred.continuity_id

    (announce, event), merged_continuity = asyncio.run(scenario())
    assert announce.startswith("event: continuity") and merged_continuity in announce
    assert event.startswith("id: 3") and "after" in event


def test_a_token_with_a_non_string_session_is_a_token_error() -> None:
    import pytest

    from agent_bridge.result_tokens import ResultTokenError, _decode_token, _encode_token

    bad = _encode_token({"kind": "position", "source": "represented", "session_id": [], "event_id": 1})
    with pytest.raises(ResultTokenError):
        _decode_token(bad, source="represented", session_id="s", kinds=frozenset({"position"}),
                      retired_ids=frozenset({"old"}))

from __future__ import annotations

import sys

from agent_dispatch.queue import Status
from agent_dispatch.verification import advance_submitted_verifications
from tests._helpers import TEST_REPO
from tests._helpers import RepoDefaultingQueue as TaskQueue


class _Bus:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, event: dict) -> None:
        self.events.append(event)


def _submitted_task(
    queue: TaskQueue,
    title: str,
    *,
    require_verification: bool,
    evaluator_ref: str | None,
) -> str:
    task = queue.create(
        title,
        require_verification=require_verification,
        evaluator_ref=evaluator_ref,
    )
    queue.claim_one("worker-1", task_id=task.id)
    queue.start(task.id, "worker-1")
    queue.complete(task.id, "worker-1")
    return task.id


def test_verification_pass_applies_confirm_abandon_and_leaves_others_alone(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.db")
    script = tmp_path / "eval.py"
    script.write_text(
        "import json, sys\n"
        "event = json.load(sys.stdin)\n"
        "title = event['task']['title']\n"
        "if 'merged' in title:\n"
        "    decision = {'decision': 'confirm', 'reason': 'merged'}\n"
        "elif 'closed' in title:\n"
        "    decision = {'decision': 'abandon', 'reason': 'closed-unmerged'}\n"
        "else:\n"
        "    decision = {'decision': 'noop', 'reason': 'still-open'}\n"
        "json.dump(decision, sys.stdout)\n",
        encoding="utf-8",
    )
    queue.register_registration(
        "evaluator",
        {
            "repo": TEST_REPO,
            "evaluator_ref": "review-loop",
            "evaluator_spec": {
                "scripts": {"review-loop": [sys.executable, str(script)]}
            },
        },
    )

    merged_id = _submitted_task(
        queue,
        "target merged",
        require_verification=True,
        evaluator_ref="review-loop",
    )
    closed_id = _submitted_task(
        queue,
        "target closed",
        require_verification=True,
        evaluator_ref="review-loop",
    )
    waiting_id = _submitted_task(
        queue,
        "still waiting",
        require_verification=True,
        evaluator_ref="review-loop",
    )
    _submitted_task(
        queue,
        "self attested",
        require_verification=False,
        evaluator_ref="review-loop",
    )
    task = queue.create(
        "started but not submitted",
        require_verification=True,
        evaluator_ref="review-loop",
    )
    queue.claim_one("worker-2", task_id=task.id)
    queue.start(task.id, "worker-2")

    bus = _Bus()
    summary = advance_submitted_verifications(queue, bus=bus)

    assert summary == {
        "checked": 3,
        "matched": 3,
        "emitted": 0,
        "confirmed": 1,
        "abandoned": 1,
        "noop": 1,
    }
    assert queue.get(merged_id).status == Status.COMPLETED
    assert queue.get(closed_id).status == Status.ABANDONED
    assert queue.get(waiting_id).status == Status.SUBMITTED
    assert [event["type"] for event in bus.events] == [
        "task.completed",
        "task.abandoned",
    ]


def test_verification_pass_respects_repo_scoped_registrations(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.db")
    script = tmp_path / "eval.py"
    script.write_text(
        "import json, sys\n"
        "json.dump({'decision': 'confirm'}, sys.stdout)\n",
        encoding="utf-8",
    )
    queue.register_registration(
        "evaluator",
        {
            "repo": TEST_REPO,
            "evaluator_ref": "review-loop",
            "evaluator_spec": {
                "scripts": {"review-loop": [sys.executable, str(script)]}
            },
        },
    )
    other_repo = "example.com/other/project"
    untouched = queue.create(
        "other repo",
        repo=other_repo,
        require_verification=True,
        evaluator_ref="review-loop",
    )
    queue.claim_one("worker-1", repo=other_repo, task_id=untouched.id)
    queue.start(untouched.id, "worker-1")
    queue.complete(untouched.id, "worker-1")

    summary = advance_submitted_verifications(queue, current_machine="lambda-core")

    assert summary["checked"] == 1
    assert summary["matched"] == 0
    assert queue.get(untouched.id).status == Status.SUBMITTED


def test_verification_pass_uses_environment_fallback(tmp_path, monkeypatch):
    queue = TaskQueue(tmp_path / "tasks.db")
    script = tmp_path / "eval.py"
    script.write_text(
        "import json, sys\n"
        "json.dump({'decision': 'confirm'}, sys.stdout)\n",
        encoding="utf-8",
    )
    queue.register_registration(
        "evaluator",
        {
            "repo": TEST_REPO,
            "evaluator_ref": "review-loop",
            "evaluator_spec": {
                "scripts": {"review-loop": [sys.executable, str(script)]}
            },
        },
        machine="lambda-core",
        env="staging",
    )
    task_id = _submitted_task(
        queue,
        "staging task",
        require_verification=True,
        evaluator_ref="review-loop",
    )

    monkeypatch.setenv("AGENT_DISPATCH_ENV", "staging")
    summary = advance_submitted_verifications(queue, current_machine="lambda-core")

    assert summary["matched"] == 1
    assert queue.get(task_id).status == Status.COMPLETED


def test_verification_pass_rotates_past_earlier_noops(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.db")
    script = tmp_path / "eval.py"
    script.write_text(
        "import json, sys\n"
        "title = json.load(sys.stdin)['task']['title']\n"
        "decision = {'decision': 'confirm'} if title == 'third' else {'decision': 'noop'}\n"
        "json.dump(decision, sys.stdout)\n",
        encoding="utf-8",
    )
    queue.register_registration(
        "evaluator",
        {
            "repo": TEST_REPO,
            "evaluator_ref": "review-loop",
            "evaluator_spec": {
                "scripts": {"review-loop": [sys.executable, str(script)]}
            },
        },
    )
    for title in ("first", "second", "third"):
        tid = _submitted_task(
            queue,
            title,
            require_verification=True,
            evaluator_ref="review-loop",
        )
        assert queue.get(tid).status == Status.SUBMITTED

    first = advance_submitted_verifications(queue, limit=2)
    second = advance_submitted_verifications(queue, limit=2)

    assert first["matched"] == 2
    assert second["matched"] == 2
    assert queue.list_verification_candidates(limit=3)[0].title in {"first", "second"}
    third = next(task for task in queue.list(status=Status.COMPLETED) if task.title == "third")
    assert third.status == Status.COMPLETED


def test_verification_emit_without_dedup_key_stays_idempotent(tmp_path):
    queue = TaskQueue(tmp_path / "tasks.db")
    script = tmp_path / "eval.py"
    script.write_text(
        "import json, sys\n"
        "json.dump({'decision': 'emit', 'title': 'follow-up', 'fields': {}}, sys.stdout)\n",
        encoding="utf-8",
    )
    queue.register_registration(
        "evaluator",
        {
            "repo": TEST_REPO,
            "evaluator_ref": "review-loop",
            "evaluator_spec": {
                "scripts": {"review-loop": [sys.executable, str(script)]}
            },
        },
    )
    task_id = _submitted_task(
        queue,
        "source task",
        require_verification=True,
        evaluator_ref="review-loop",
    )

    first = advance_submitted_verifications(queue)
    second = advance_submitted_verifications(queue)

    followups = [task for task in queue.list(status=Status.QUEUED) if task.title == "follow-up"]
    assert first["emitted"] == 1
    assert second["emitted"] == 1
    assert len(followups) == 1
    assert followups[0].dedup_key is not None
    assert queue.get(task_id).status == Status.SUBMITTED

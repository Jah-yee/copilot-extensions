"""Coordinator-side verification pass for submitted tasks.

This is the recurring, bounded loop behind the verification gate:
submitted tasks explicitly marked ``require_verification`` and bound to a
registered ``evaluator_ref`` are re-run through that evaluator until it
confirms, abandons, or keeps waiting.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from . import telemetry
from .events import EventBus
from .producers.evaluator import (
    EvaluatorError,
    apply_decisions,
    load_registration_evaluator,
)
from .queue import Status, TaskQueue
from .registrations import RegistrationKind

log = logging.getLogger("agent-dispatch.verification")


def _event_task_dict(task: dict[str, Any]) -> dict[str, Any]:
    result = dict(task)
    result["has_result"] = result.pop("result", None) is not None or bool(
        result.get("has_result")
    )
    return result


def _publish(bus: EventBus | None, event_type: str, task: dict[str, Any]) -> None:
    if bus is None:
        return
    event_task = _event_task_dict(task)
    bus.publish({"type": event_type, "task": event_task})
    telemetry.emit(telemetry.task_lifecycle_event(event_type, event_task))


def _active_evaluators(queue: TaskQueue) -> list[tuple[dict[str, Any], Any]]:
    registry: list[tuple[dict[str, Any], Any]] = []
    for record in queue.list_registrations(
        kind=RegistrationKind.EVALUATOR,
        include_paused=False,
    ):
        spec = record.spec or {}
        evaluator_ref = spec.get("evaluator_ref")
        if not isinstance(evaluator_ref, str) or not evaluator_ref:
            continue
        try:
            loaded = load_registration_evaluator(spec)
        except EvaluatorError as exc:
            log.warning(
                "skipping evaluator registration %s (%s): %s",
                record.id,
                evaluator_ref,
                exc,
            )
            continue
        registry.append((spec, loaded))
    return registry


def _evaluator_for_task(
    registrations: list[tuple[dict[str, Any], Any]],
    *,
    repo: str | None,
    evaluator_ref: str,
) -> Any | None:
    exact: list[Any] = []
    global_matches: list[Any] = []
    for spec, loaded in registrations:
        if spec.get("evaluator_ref") != evaluator_ref:
            continue
        if spec.get("all_repos"):
            global_matches.append(loaded)
            continue
        if spec.get("repo") == repo:
            exact.append(loaded)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        log.warning(
            "skipping verification for evaluator_ref %s in repo %s: multiple repo-scoped registrations",
            evaluator_ref,
            repo,
        )
        return None
    if len(global_matches) == 1:
        return global_matches[0]
    if len(global_matches) > 1:
        log.warning(
            "skipping verification for evaluator_ref %s: multiple all-repos registrations",
            evaluator_ref,
        )
    return None


def advance_submitted_verifications(
    queue: TaskQueue,
    *,
    bus: EventBus | None = None,
    limit: int = 200,
) -> dict[str, int]:
    """Evaluate every submitted, verification-gated task with a registered evaluator."""
    registrations = _active_evaluators(queue)
    summary = {
        "checked": 0,
        "matched": 0,
        "emitted": 0,
        "confirmed": 0,
        "abandoned": 0,
        "noop": 0,
    }
    if not registrations:
        return summary

    for task in queue.list_verification_candidates(limit=limit):
        summary["checked"] += 1
        evaluator_ref = task.evaluator_ref or ""
        evaluator = _evaluator_for_task(
            registrations,
            repo=task.repo,
            evaluator_ref=evaluator_ref,
        )
        if evaluator is None:
            continue
        summary["matched"] += 1
        event = {"type": "task.submitted", "task": asdict(task)}

        def creator(title: str, **fields: Any) -> dict[str, Any]:
            proposed = bool(fields.pop("proposed", False))
            outcome = (
                queue.propose_outcome(title, **fields)
                if proposed
                else queue.create_outcome(title, **fields)
            )
            created = asdict(outcome.task)
            if outcome.event_type is not None:
                _publish(bus, outcome.event_type, created)
            return created

        def confirmer(task_id: str, *, actor: str | None = None, **_kwargs: Any) -> dict[str, Any]:
            current = queue.get(task_id)
            if current is not None and current.status == Status.COMPLETED:
                return asdict(current)
            confirmed = asdict(queue.confirm(task_id, actor=actor))
            _publish(bus, "task.completed", confirmed)
            return confirmed

        def abandoner(
            task_id: str,
            *,
            reason: str | None = None,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            outcome = queue.abandon_with_outcome(
                task_id,
                permitted=True,
                reason=reason,
                expected_status=Status.SUBMITTED,
            )
            abandoned = asdict(outcome.task)
            if outcome.event_type is not None:
                _publish(bus, outcome.event_type, abandoned)
            return abandoned

        try:
            results = apply_decisions(
                evaluator.evaluate(event),
                creator=creator,
                repo=task.repo,
                task_id=task.id,
                confirmer=confirmer,
                abandoner=abandoner,
            )
        except Exception:
            log.exception(
                "verification pass failed for task %s via evaluator_ref %s",
                task.id,
                task.evaluator_ref,
            )
            continue
        for result in results:
            match result.get("decision"):
                case "emit":
                    if result.get("created"):
                        summary["emitted"] += 1
                case "confirm":
                    if result.get("completed"):
                        summary["confirmed"] += 1
                    else:
                        summary["noop"] += 1
                case "abandon":
                    if result.get("abandoned"):
                        summary["abandoned"] += 1
                    else:
                        summary["noop"] += 1
                case _:
                    summary["noop"] += 1
    return summary

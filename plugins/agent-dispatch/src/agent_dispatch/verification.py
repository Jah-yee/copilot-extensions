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


def _active_evaluators(queue: TaskQueue) -> dict[str, Any]:
    registry: dict[str, Any] = {}
    duplicates: set[str] = set()
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
        if evaluator_ref in registry:
            duplicates.add(evaluator_ref)
            registry.pop(evaluator_ref, None)
            continue
        registry[evaluator_ref] = loaded
    for evaluator_ref in sorted(duplicates):
        log.warning(
            "skipping verification for evaluator_ref %s: multiple active evaluator registrations",
            evaluator_ref,
        )
    return registry


def advance_submitted_verifications(
    queue: TaskQueue,
    *,
    bus: EventBus | None = None,
    limit: int = 200,
) -> dict[str, int]:
    """Evaluate every submitted, verification-gated task with a registered evaluator."""
    evaluators = _active_evaluators(queue)
    summary = {
        "checked": 0,
        "matched": 0,
        "emitted": 0,
        "confirmed": 0,
        "abandoned": 0,
        "noop": 0,
    }
    if not evaluators:
        return summary

    for task in queue.list_verification_candidates(limit=limit):
        summary["checked"] += 1
        evaluator = evaluators.get(task.evaluator_ref or "")
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

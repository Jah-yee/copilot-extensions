"""Copilot pane readiness helpers for safe detached seed injection."""

from __future__ import annotations


def input_region(capture: str) -> str:
    """Bottom live-input region, not scrollback transcript history."""
    lines = [line.rstrip() for line in capture.splitlines() if line.strip()]
    return "\n".join(lines[-8:])


def is_busy(region: str) -> bool:
    """True when the live region is visibly not accepting input yet."""
    low = region.lower()
    return any(
        phrase in low
        for phrase in (
            "resuming session",
            "loading",
            "starting",
            "initializing",
            "authenticating",
            "connecting",
        )
    )


def ready_signature(capture: str) -> str | None:
    """Stable cue for a live Copilot input prompt, or ``None`` when not ready."""
    region = input_region(capture)
    low = region.lower()
    if "enter to select" in low or is_busy(region):
        return None
    # Copilot CLI >= 1.0.89 boxed input. Both box rails must be in the live
    # bottom region so an old transcript drawing cannot satisfy readiness.
    if "╻▄" in region and "╹▀" in region:
        return "boxed-input"
    # Older Copilot builds expose a footer while the input is live. Require
    # the footer in the bottom region; a bare shell prompt that happens to use
    # the same caret glyph is not enough.
    if "esc" in low and "interrupt" in low:
        return "interrupt-footer"
    return None

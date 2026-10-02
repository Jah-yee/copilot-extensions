"""Copilot pane readiness helpers for safe detached seed injection."""

from __future__ import annotations

import re


_BUSY_STATUS = re.compile(
    r"^\s*(?:[^\w\s~/.]\s*)?"
    r"(?:resuming session|loading|starting|initializing|authenticating|connecting)\b",
    re.IGNORECASE,
)


def input_region(capture: str) -> str:
    """Bottom live-input region, not scrollback transcript history.

    With the boxed input drawn, that is the box and its footer plus the two
    lines above it (the status line and the directory banner); otherwise the
    last few lines of the pane.
    """
    lines = [line.rstrip() for line in capture.splitlines() if line.strip()]
    rail = max((i for i, line in enumerate(lines) if "╻▄" in line), default=None)
    if rail is not None:
        return "\n".join(lines[max(0, rail - 2):])
    return "\n".join(lines[-8:])


def is_busy(region: str) -> bool:
    """True when a status line in the live region says Copilot isn't taking
    input yet: the line *starts* with a busy verb (after an optional spinner
    glyph), so a banner path or a transcript sentence that merely contains the
    word doesn't count."""
    return any(_BUSY_STATUS.match(line) for line in region.splitlines())


#: Shell prompts (incl. attached ``PS C:\repo>`` / ``C:\repo>``) and exit lines.
_SHELL_TAIL = re.compile(
    r"[❯$#%]\s*$|(?:^|\s)>\s*$|^\s*(?:PS\b[^>]*|[A-Za-z]:\\[^>]*)>\s*$|\bexited\b",
    re.IGNORECASE,
)
#: Lines Copilot draws under its input box (the key-hint footer).
_MAX_FOOTER_LINES = 2
#: A Copilot key-hint footer row (e.g. "← open sidebar · / commands · ? help"):
#: anything else under the box (``Connection closed``, later output) means the
#: box is stale scrollback, not live input.
_FOOTER_ROW = re.compile(
    r"\s·\s|\b(?:esc|ctrl|shift|tab|enter|help|commands|sidebar)\b", re.IGNORECASE,
)


def _live_box(capture: str) -> bool:
    """A complete input box (top rail, then bottom rail) with nothing under it
    but Copilot's footer rows. A box left in the scrollback above a shell
    prompt (Copilot exited) or any other later output is not live input."""
    lines = [line.rstrip() for line in capture.splitlines() if line.strip()]
    top = max((i for i, line in enumerate(lines) if "╻▄" in line), default=None)
    if top is None:
        return False
    bottom = next((i for i in range(top + 1, len(lines)) if "╹▀" in lines[i]), None)
    if bottom is None:
        return False
    tail = lines[bottom + 1:]
    return len(tail) <= _MAX_FOOTER_LINES and all(
        _FOOTER_ROW.search(line) and not _SHELL_TAIL.search(line) for line in tail
    )


def _live_footer(region: str) -> bool:
    """The "esc ... interrupt" footer as the live bottom line: anything below it
    (a prompt, an exit line, ``Connection closed``) means it is stale."""
    lines = [line for line in region.splitlines() if line.strip()]
    return bool(lines) and "esc" in lines[-1].lower() and "interrupt" in lines[-1].lower()


def ready_signature(capture: str) -> str | None:
    """Stable cue for a live Copilot input prompt, or ``None`` when not ready."""
    region = input_region(capture)
    low = region.lower()
    if "enter to select" in low or is_busy(region):
        return None
    # Copilot CLI >= 1.0.89 boxed input, drawn at the live bottom of the pane.
    if _live_box(capture):
        return "boxed-input"
    # Older Copilot builds expose a footer while the input is live. Require
    # the footer in the bottom region; a bare shell prompt that happens to use
    # the same caret glyph is not enough.
    if _live_footer(region):
        return "interrupt-footer"
    return None

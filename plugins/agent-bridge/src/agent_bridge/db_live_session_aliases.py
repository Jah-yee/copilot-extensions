"""Live-session alias helpers."""

from __future__ import annotations

#: SQL resolving a (possibly retired) live-session id to its current id inside
#: the same statement as a read or write, so a concurrent rename can't slip in
#: between lookup and write. Aliases are kept one hop deep: each rename
#: re-points existing aliases at the newest id. Binds the id twice.
CANONICAL_SESSION_SQL = (
    "COALESCE((SELECT target_session_id FROM live_session_aliases "
    "WHERE alias_session_id = ?), ?)"
)

from typing import Any


def inherit_cli_mode_claim_for_session_id_change(
    db: Any, worktree_id: str, session_id: str, *, now: float
) -> str | None:
    """Move a CLI-mode session's handle and claim to ``session_id`` when the
    same process re-registers under a new conversation id (a resume).

    The predecessor is found from durable live registrations, not the launch
    reservation (which a launcher may already have released, or which may have
    expired): same worktree and machine, ``cli_mode``, and the *same, known*
    PID. A missing PID on either side never counts as a match.
    """
    conn = db._get_conn()
    with db._write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            successor = conn.execute(
                "SELECT * FROM live_sessions WHERE session_id=? "
                "AND worktree_id=?",
                (session_id, worktree_id),
            ).fetchone()
            if successor is None or successor["pid"] is None:
                conn.rollback()
                return None
            predecessor = conn.execute(
                "SELECT * FROM live_sessions WHERE worktree_id=? "
                "AND session_id != ? AND cli_mode=1 AND pid IS NOT NULL "
                "AND status != 'taken-over' "
                "AND pid=? AND machine IS ? "
                "ORDER BY updated_at DESC LIMIT 1",
                (worktree_id, session_id, successor["pid"], successor["machine"]),
            ).fetchone()
            if predecessor is None:
                conn.rollback()
                return None
            predecessor_id = predecessor["session_id"]
            reservation = conn.execute(
                "SELECT venue FROM cli_mode_reservations "
                "WHERE worktree_id=? AND claimed_by_session_id=?",
                (worktree_id, predecessor_id),
            ).fetchone()
            venue = (
                (reservation["venue"] if reservation is not None else None)
                or predecessor["venue"] or successor["venue"]
            )
            driven_by = successor["driven_by"] or predecessor["driven_by"]
            conn.execute(
                "UPDATE cli_mode_reservations SET claimed_by_session_id=? "
                "WHERE worktree_id=? AND claimed_by_session_id=?",
                (session_id, worktree_id, predecessor_id),
            )
            conn.execute(
                "UPDATE live_sessions SET cli_mode=1, venue=?, driven_by=?, "
                "updated_at=? WHERE session_id=?",
                (venue, driven_by, now, session_id),
            )
            conn.execute(
                "INSERT INTO live_session_aliases "
                "(alias_session_id, target_session_id, created_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(alias_session_id) DO UPDATE SET "
                "target_session_id=excluded.target_session_id, "
                "created_at=excluded.created_at",
                (predecessor_id, session_id, now),
            )
            conn.execute(
                "UPDATE live_session_aliases SET target_session_id=? "
                "WHERE target_session_id=?",
                (session_id, predecessor_id),
            )
            conn.execute(
                "UPDATE live_messages SET session_id=? WHERE session_id=?",
                (session_id, predecessor_id),
            )
            conn.execute(
                "DELETE FROM live_sessions WHERE session_id=?",
                (predecessor_id,),
            )
            conn.commit()
            return predecessor_id
        except Exception:
            conn.rollback()
            raise

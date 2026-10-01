"""Live-session alias helpers."""

from __future__ import annotations

from typing import Any


def inherit_cli_mode_claim_for_session_id_change(
    db: Any, worktree_id: str, session_id: str, *, now: float
) -> str | None:
    """Move a claimed CLI-mode scope from a placeholder id to ``session_id``."""
    conn = db._get_conn()
    with db._write_lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            reservation = conn.execute(
                "SELECT * FROM cli_mode_reservations "
                "WHERE worktree_id=? AND claimed_by_session_id IS NOT NULL "
                "AND claimed_by_session_id != ? AND expires_at > ?",
                (worktree_id, session_id, now),
            ).fetchone()
            if reservation is None:
                conn.rollback()
                return None
            predecessor_id = reservation["claimed_by_session_id"]
            predecessor = conn.execute(
                "SELECT * FROM live_sessions WHERE session_id=? "
                "AND worktree_id=? AND cli_mode=1",
                (predecessor_id, worktree_id),
            ).fetchone()
            successor = conn.execute(
                "SELECT * FROM live_sessions WHERE session_id=? "
                "AND worktree_id=?",
                (session_id, worktree_id),
            ).fetchone()
            if predecessor is None or successor is None:
                conn.rollback()
                return None
            pred_pid = predecessor["pid"]
            succ_pid = successor["pid"]
            if pred_pid is not None and succ_pid is not None and pred_pid != succ_pid:
                conn.rollback()
                return None
            venue = reservation["venue"] or predecessor["venue"] or successor["venue"]
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
                "UPDATE live_messages SET session_id=? "
                "WHERE session_id=? AND delivered_at IS NULL",
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

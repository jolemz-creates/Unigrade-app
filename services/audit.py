"""
services/audit.py — UniGrade Audit Logger

Single responsibility: write records to the audit_log table.
This module NEVER raises exceptions to its callers. If the insert fails,
it degrades silently to a stderr print — the caller's flow must never be
interrupted by a logging failure.

All other modules import log_to_audit() from here. Do not call the DB
directly from other service or page modules for audit purposes.
"""

import json
import sys
from typing import Any

from models.db_manager import get_connection


def log_to_audit(
    action: str,
    user_id: str | int | None = None,
    exam_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """
    Insert a record into audit_log.

    Parameters
    ----------
    action   : Short description of the event, e.g. "Manual override on Q3".
                Must be non-empty — the DB column is NOT NULL.
    user_id  : Matric number (str) for students; lecturers.id (int) for staff.
                Stored as TEXT in the DB; converted here so the caller never
                has to think about it.
    exam_id  : FK to exams.id. Pass None when the event is not exam-specific
                (e.g., login events, registration).
    details  : Arbitrary key-value context. Serialised to a JSON string before
                the INSERT. Pass None when there is no extra context.

    Failure contract
    ----------------
    This function swallows ALL exceptions. On failure it writes a single line
    to stderr — it never re-raises, never crashes the caller, and never returns
    an error value. Callers must not branch on its return value.
    """
    if not action or not action.strip():
        # Refuse a blank action string — the column is NOT NULL and an empty
        # string is meaningless in the audit trail.
        print(
            "[audit.py] log_to_audit called with empty action — skipping.",
            file=sys.stderr,
        )
        return

    # Serialise details dict → JSON string (or leave as None for SQL NULL).
    details_json: str | None = None
    if details is not None:
        try:
            details_json = json.dumps(details, ensure_ascii=False, default=str)
        except (TypeError, ValueError) as exc:
            # Serialisation failed (e.g., non-serialisable object). Log what we
            # can, replacing the bad payload with an error note.
            details_json = json.dumps(
                {"_serialisation_error": str(exc), "_raw_repr": repr(details)[:500]}
            )

    # Convert user_id to str so it fits the TEXT column regardless of whether
    # the caller passes a matric string ("21/52CS001") or an int lecturer id.
    user_id_str: str | None = str(user_id) if user_id is not None else None

    try:
        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO audit_log (action, user_id, exam_id, details)
                VALUES (?, ?, ?, ?)
                """,
                (action.strip(), user_id_str, exam_id, details_json),
            )
            # get_connection() returns a connection whose context manager commits
            # on success and rolls back on exception (standard sqlite3 behaviour).
    except Exception as exc:  # noqa: BLE001 — intentional broad catch
        # Last resort: write to stderr. Do NOT raise.
        print(
            f"[audit.py] CRITICAL — failed to write audit log.\n"
            f"  action   : {action!r}\n"
            f"  user_id  : {user_id_str!r}\n"
            f"  exam_id  : {exam_id!r}\n"
            f"  details  : {details_json!r}\n"
            f"  error    : {exc!r}",
            file=sys.stderr,
        )
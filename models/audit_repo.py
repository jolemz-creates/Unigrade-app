# models/audit_repo.py
# UniGrade package — Audit log repository
#
# Pure data access layer for the `audit_log` table.
# INSERT-only writes (audit records are immutable once created).
# Read path provided for Chief Examiner audit view.
#
# IMPORTANT: insert_audit_log() MUST NEVER RAISE under any circumstance.
# It is called from error handlers, grading pipelines, and override logging.
# A secondary failure here would mask the original error and could crash a
# student's live exam session. Swallowing exceptions silently (with stderr
# as a last resort) is the correct behaviour for this function specifically.
#
# Callers that already have a details dict should prefer log_to_audit() from
# services/audit.py, which handles dict→JSON serialisation before calling here.

import sqlite3
import sys
from models.db_manager import get_connection


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


def insert_audit_log(
    action: str,
    user_id: str | int | None = None,
    exam_id: int | None = None,
    details_json: str | None = None,
) -> None:
    """
    INSERT a single row into audit_log.

    Parameters
    ----------
    action      : Short description of the event, e.g. "Manual override on Q3".
    user_id     : Matric number (str) for students; lecturers.id (int) for staff.
                  Stored as TEXT — both types are accepted and coerced via str().
    exam_id     : FK to exams.id. NULL for system-level events not tied to an exam.
    details_json: Pre-serialised JSON string for additional context.
                  The caller is responsible for serialisation (see services/audit.py).

    This function NEVER raises. Any exception is printed to stderr and
    swallowed. Do not add try/except logic around calls to this function —
    the safety net is already here.
    """
    sql = """
        INSERT INTO audit_log (action, user_id, exam_id, details)
        VALUES (?, ?, ?, ?)
    """
    try:
        # Normalise user_id to str for consistent TEXT storage, preserving NULL.
        stored_user_id = str(user_id) if user_id is not None else None
        with get_connection() as conn:
            conn.execute(sql, (action, stored_user_id, exam_id, details_json))
    except Exception as exc:  # noqa: BLE001 — intentional broad catch
        # Last resort: print to stderr. Do NOT re-raise.
        print(
            f"[UniGrade audit_repo] FAILED to write audit log — "
            f"action='{action}' user_id={user_id} exam_id={exam_id} error={exc}",
            file=sys.stderr,
        )


def get_audit_logs(
    exam_id: int | None = None,
    limit: int = 200,
) -> list[dict]:
    """
    Return audit log entries, newest first.

    Parameters
    ----------
    exam_id : If provided, return only entries for that exam.
              If None, return entries across all exams.
    limit   : Maximum number of rows to return. Default 200.

    Unlike insert_audit_log, this function does NOT swallow exceptions.
    A read failure should surface to the caller (e.g. the Chief Examiner
    audit page) so it can display an error rather than a silently empty table.
    """
    if exam_id is not None:
        sql = """
            SELECT * FROM audit_log
            WHERE exam_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
        """
        params = (exam_id, limit)
    else:
        sql = """
            SELECT * FROM audit_log
            ORDER BY timestamp DESC
            LIMIT ?
        """
        params = (limit,)

    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return _rows_to_dicts(rows)
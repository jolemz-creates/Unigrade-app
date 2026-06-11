# models/student_repo.py
# UniGrade package — Student repository
#
# Pure data access layer for `students`, `student_responses`, and `exam_sessions`.
# NO business logic, NO bcrypt, NO Groq calls, NO session state.
# All functions use get_connection() from models/db_manager.py.

import sqlite3
from datetime import datetime, timedelta
from models.db_manager import get_connection

# Valid terminal statuses for exam submission.
_VALID_SUBMIT_STATUSES = {"Submitted", "Auto-Submitted"}

# Statuses that mean the exam is over and responses are locked.
# NOTE: The spec mentions checking for 'Submitted' only, but 'Auto-Submitted'
# is semantically identical (exam is closed, answers locked). Excluding it
# would make auto-submitted responses permanently un-overridable by lecturers,
# which is a product bug. Both are accepted here.
_LOCKED_STATUSES = {"Submitted", "Auto-Submitted"}


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


def _now_str() -> str:
    """Current local time as an ISO 8601 string (second precision)."""
    return datetime.now().isoformat(sep=" ", timespec="seconds")


# ---------------------------------------------------------------------------
# Student functions
# ---------------------------------------------------------------------------

def get_student_by_matric(matric_no: str) -> dict | None:
    """
    Fetch a single student row by matric number (primary key).

    Returns a dict of all columns, or None if not found.
    """
    sql = "SELECT * FROM students WHERE matric_no = ? LIMIT 1"
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, (matric_no,)).fetchone()
    return _row_to_dict(row) if row is not None else None


def create_student(
    matric_no: str,
    name: str,
    department: str,
    level: int,
    email: str,
) -> None:
    """
    INSERT a student record. Silently no-ops if the matric_no already exists.

    INSERT OR IGNORE is intentional — students may be pre-registered or
    log in via an external registry sync; a duplicate insert must never crash
    the student's session.
    """
    sql = """
        INSERT OR IGNORE INTO students (matric_no, name, department, level, email)
        VALUES (?, ?, ?, ?, ?)
    """
    with get_connection() as conn:
        conn.execute(sql, (matric_no, name, department, level, email))


# ---------------------------------------------------------------------------
# Exam session functions
# ---------------------------------------------------------------------------

def start_exam_session(
    exam_id: int,
    student_id: str,
    duration_minutes: int,
    ip_address: str,
) -> dict:
    """
    Create or resume an exam session for the given student.

    Behaviour:
    - If no session exists: INSERT a new 'In Progress' session and return it.
    - If session exists with status='In Progress': return the existing session
      (resume logic — the timer is preserved via the stored end_time).
    - If session exists with status in {'Submitted', 'Auto-Submitted'}:
      raise ValueError. The exam is closed.

    Race condition handling: if two concurrent requests try to create the same
    session simultaneously, one INSERT will hit the UNIQUE(exam_id, student_id)
    constraint. The IntegrityError is caught and treated as a resume — we
    SELECT and return the row the winner already created.

    Returns a dict representing the exam_sessions row.
    """
    fetch_sql = """
        SELECT * FROM exam_sessions
        WHERE exam_id = ? AND student_id = ?
        LIMIT 1
    """
    insert_sql = """
        INSERT INTO exam_sessions
            (exam_id, student_id, start_time, end_time, status, ip_address)
        VALUES
            (?, ?, ?, ?, 'In Progress', ?)
    """

    with get_connection() as conn:
        conn.row_factory = sqlite3.Row

        # Check for an existing session first.
        existing = conn.execute(fetch_sql, (exam_id, student_id)).fetchone()

        if existing is not None:
            session = _row_to_dict(existing)
            if session["status"] in _LOCKED_STATUSES:
                raise ValueError(
                    f"Exam already submitted (status='{session['status']}')."
                    " Responses are locked."
                )
            # Status is 'In Progress' — resume. Return as-is; end_time is intact.
            return session

        # No existing session — create one.
        start_time = datetime.now()
        end_time = start_time + timedelta(minutes=duration_minutes)

        start_str = start_time.isoformat(sep=" ", timespec="seconds")
        end_str = end_time.isoformat(sep=" ", timespec="seconds")

        try:
            conn.execute(insert_sql, (exam_id, student_id, start_str, end_str, ip_address))
        except sqlite3.IntegrityError:
            # Concurrent insert won the race — fetch and return what they created.
            existing = conn.execute(fetch_sql, (exam_id, student_id)).fetchone()
            if existing is None:
                # Should be impossible, but guard against it.
                raise RuntimeError(
                    "Concurrent session creation failed and no row was found. "
                    "This is a database integrity error."
                )
            session = _row_to_dict(existing)
            if session["status"] in _LOCKED_STATUSES:
                raise ValueError(
                    f"Exam already submitted (status='{session['status']}')."
                    " Responses are locked."
                )
            return session

        # Fetch the newly inserted row so we return a complete dict (with id, etc.).
        new_row = conn.execute(fetch_sql, (exam_id, student_id)).fetchone()
        return _row_to_dict(new_row)


def get_active_session(exam_id: int, student_id: str) -> dict | None:
    """
    Return the 'In Progress' session for a student in a given exam, or None.

    Used on page load to detect whether a student has an unfinished exam
    and should be resumed rather than starting fresh.
    """
    sql = """
        SELECT * FROM exam_sessions
        WHERE exam_id = ? AND student_id = ? AND status = 'In Progress'
        LIMIT 1
    """
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, (exam_id, student_id)).fetchone()
    return _row_to_dict(row) if row is not None else None


def get_in_progress_session_for_student(student_id: str) -> dict | None:
    """
    Return any 'In Progress' exam session for this student, regardless of exam.

    Used on re-login (Phase 4 session resume): session state has been wiped
    by logout, so we don't know the exam_id. This query finds the interrupted
    session by student alone, giving us both the exam_id and the authoritative
    end_time needed to decide whether to resume or auto-submit.

    Assumes at most one active session per student at a time (enforced by the
    app — students may not start a second exam while one is In Progress).
    Returns None if no interrupted session exists.
    """
    sql = """
        SELECT * FROM exam_sessions
        WHERE student_id = ? AND status = 'In Progress'
        ORDER BY start_time DESC
        LIMIT 1
    """
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, (student_id,)).fetchone()
    return _row_to_dict(row) if row is not None else None


def submit_exam_session(
    exam_id: int,
    student_id: str,
    status: str = "Submitted",
) -> None:
    """
    Mark an exam session as submitted.

    The `status` parameter must be 'Submitted' (manual submit) or
    'Auto-Submitted' (timer expiry). Raises ValueError for any other value.

    This does NOT lock student_responses rows directly — the locked state is
    inferred from this status field wherever the lock check is needed.
    """
    if status not in _VALID_SUBMIT_STATUSES:
        raise ValueError(
            f"Invalid submit status '{status}'."
            f" Must be one of: {_VALID_SUBMIT_STATUSES}."
        )
    sql = """
        UPDATE exam_sessions
        SET status = ?
        WHERE exam_id = ? AND student_id = ?
    """
    with get_connection() as conn:
        conn.execute(sql, (status, exam_id, student_id))


def autosave_session(exam_id: int, student_id: str) -> None:
    """
    Stamp the current timestamp onto exam_sessions.last_autosave.

    Called every autosave_interval seconds from the exam hall. Does not
    persist answer content — draft answers live in st.session_state.exam_answers
    and are written to student_responses only on final submission.
    """
    sql = """
        UPDATE exam_sessions
        SET last_autosave = ?
        WHERE exam_id = ? AND student_id = ?
    """
    with get_connection() as conn:
        conn.execute(sql, (_now_str(), exam_id, student_id))


# ---------------------------------------------------------------------------
# Student response functions
# ---------------------------------------------------------------------------

def save_responses_batch(responses: list[dict]) -> None:
    """
    Write a batch of student responses in a single transaction.

    Each dict must have keys:
        exam_id, student_id, question_id, answer_text, sanitized_text, submitted_at

    INSERT OR REPLACE: if a row already exists for (exam_id, student_id, question_id),
    it is replaced entirely. This is intentional — this function is called at
    final submission, before grading. No ai_score or manual_override exists yet.

    WARNING: Do NOT call this for autosave of draft answers — INSERT OR REPLACE
    would wipe any ai_score already written. For draft autosave, a separate
    UPSERT targeting only answer_text/sanitized_text would be needed.

    All rows commit together; any failure rolls back the entire batch.
    """
    sql = """
        INSERT OR REPLACE INTO student_responses
            (exam_id, student_id, question_id, answer_text, sanitized_text, submitted_at)
        VALUES
            (:exam_id, :student_id, :question_id, :answer_text, :sanitized_text, :submitted_at)
    """
    with get_connection() as conn:
        conn.executemany(sql, responses)


def update_response_grades(grades: list[dict]) -> None:
    """
    Write AI grading results back to student_responses in a single transaction.

    Each dict must have keys:
        exam_id, student_id, question_id, ai_score, ai_feedback, ai_confidence

    Matches rows by the natural key (exam_id, student_id, question_id) —
    not by the surrogate id — so this is safe to call even if response ids
    are not available in the grader's context.

    All rows commit together; any failure rolls back the entire batch.
    """
    sql = """
        UPDATE student_responses
        SET ai_score      = :ai_score,
            ai_feedback   = :ai_feedback,
            ai_confidence = :ai_confidence
        WHERE exam_id    = :exam_id
          AND student_id = :student_id
          AND question_id = :question_id
    """
    with get_connection() as conn:
        conn.executemany(sql, grades)


def get_responses_for_exam(exam_id: int) -> list[dict]:
    """
    Return all student_responses rows for a given exam.

    Intended for the lecturer grading review dashboard. Returns every
    student's responses — the page layer is responsible for any filtering
    or grouping by student / question.
    """
    sql = """
        SELECT * FROM student_responses
        WHERE exam_id = ?
        ORDER BY student_id, question_id
    """
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (exam_id,)).fetchall()
    return _rows_to_dicts(rows)


def get_responses_for_student(exam_id: int, student_id: str) -> list[dict]:
    """
    Return one student's responses for a given exam.

    Intended for the student results view. Returns only this student's rows —
    no cross-student data exposure is possible from this query.
    """
    sql = """
        SELECT * FROM student_responses
        WHERE exam_id = ? AND student_id = ?
        ORDER BY question_id
    """
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (exam_id, student_id)).fetchall()
    return _rows_to_dicts(rows)


def apply_manual_override(
    response_id: int,
    score: float,
    overridden_by: int,
    reason: str,
) -> None:
    """
    Write a lecturer's manual score override to a student_responses row.

    LOCK ENFORCEMENT: Overrides are only permitted when the associated
    exam_sessions row has status in {'Submitted', 'Auto-Submitted'}.
    Attempting to override while a session is still 'In Progress' raises
    ValueError — this prevents mid-exam score tampering.

    The session status is resolved via a JOIN from the response_id, so the
    caller does not need to know the exam_id or student_id separately.

    Raises:
        ValueError — if the session is not in a locked state.
        sqlite3.IntegrityError — if overridden_by is not a valid lecturers.id FK.
    """
    # Resolve session status for this response without requiring the caller
    # to supply exam_id / student_id redundantly.
    check_sql = """
        SELECT es.status
        FROM exam_sessions es
        JOIN student_responses sr
          ON sr.exam_id = es.exam_id AND sr.student_id = es.student_id
        WHERE sr.id = ?
        LIMIT 1
    """
    update_sql = """
        UPDATE student_responses
        SET manual_override = ?,
            overridden_by   = ?,
            override_reason = ?
        WHERE id = ?
    """

    with get_connection() as conn:
        row = conn.execute(check_sql, (response_id,)).fetchone()

        if row is None:
            raise ValueError(
                f"No student_response found with id={response_id},"
                " or it has no associated exam_session."
            )

        session_status = row[0]
        if session_status not in _LOCKED_STATUSES:
            raise ValueError(
                f"Cannot override score: exam session is '{session_status}'."
                " Overrides are only allowed after the student has submitted."
            )

        conn.execute(update_sql, (score, overridden_by, reason, response_id))
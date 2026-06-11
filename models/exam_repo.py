# models/exam_repo.py
# UniGrade package — Exam and Question repository
#
# Pure data access layer for the `exams` and `questions` tables.
# NO business logic, NO bcrypt, NO Groq calls, NO session state.
# All functions use get_connection() from models/db_manager.py.

import sqlite3
from datetime import datetime
from models.db_manager import get_connection

# Valid statuses — mirrors the DB CHECK constraint.
_VALID_EXAM_STATUSES = {"Draft", "Published", "Closed"}


def _row_to_dict(row: sqlite3.Row) -> dict:
    return dict(row)


def _rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# session_code helpers
# ---------------------------------------------------------------------------

def _generate_session_code(year: int, course_code: str, title: str) -> str:
    """Build the base session_code per the spec formula."""
    return f"{year}-{course_code}-{title[:6].upper()}"


def _resolve_unique_session_code(conn: sqlite3.Connection, base_code: str) -> str:
    """
    Return a session_code that does not already exist in the exams table.

    Tries the base code first. If already taken, appends -2, -3, … up to -99.
    Raises RuntimeError (not IntegrityError) if all candidates are exhausted —
    this is a degenerate case that should never occur in practice.
    """
    candidate = base_code
    row = conn.execute(
        "SELECT 1 FROM exams WHERE session_code = ? LIMIT 1", (candidate,)
    ).fetchone()

    if row is None:
        return candidate

    for suffix in range(2, 100):
        candidate = f"{base_code}-{suffix}"
        row = conn.execute(
            "SELECT 1 FROM exams WHERE session_code = ? LIMIT 1", (candidate,)
        ).fetchone()
        if row is None:
            return candidate

    raise RuntimeError(
        f"Could not generate a unique session_code for base '{base_code}'. "
        "Too many exams with the same year, course code, and title prefix."
    )


# ---------------------------------------------------------------------------
# Exam functions
# ---------------------------------------------------------------------------

def create_exam(
    lecturer_id: int,
    course_code: str,
    title: str,
    instructions: str,
    duration: int,
) -> int:
    """
    INSERT a new exam record with status='Draft' and a generated session_code.

    session_code format: "{year}-{course_code}-{title[:6].upper()}"
    A suffix (-2, -3, …) is appended automatically on collision.

    Returns the new exam's integer id (lastrowid).
    Raises sqlite3.IntegrityError if lecturer_id is not a valid FK reference.
    """
    year = datetime.now().year
    base_code = _generate_session_code(year, course_code, title)

    sql = """
        INSERT INTO exams
            (lecturer_id, course_code, title, instructions, duration, session_code, status)
        VALUES
            (?, ?, ?, ?, ?, ?, 'Draft')
    """
    with get_connection() as conn:
        session_code = _resolve_unique_session_code(conn, base_code)
        cursor = conn.execute(
            sql, (lecturer_id, course_code, title, instructions, duration, session_code)
        )
        return cursor.lastrowid


def get_exams_by_lecturer(lecturer_id: int) -> list[dict]:
    """
    Return all exams owned by the given lecturer_id, ordered newest first.
    """
    sql = """
        SELECT * FROM exams
        WHERE lecturer_id = ?
        ORDER BY created_at DESC
    """
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (lecturer_id,)).fetchall()
    return _rows_to_dicts(rows)


def get_exam_by_id(exam_id: int) -> dict | None:
    """
    Fetch a single exam row by primary key.

    Returns a dict of all columns, or None if not found.
    """
    sql = "SELECT * FROM exams WHERE id = ? LIMIT 1"
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, (exam_id,)).fetchone()
    return _row_to_dict(row) if row is not None else None


def get_published_exams_by_department(department: str) -> list[dict]:
    """
    Return all exams that are Published AND chief_approved=TRUE, filtered
    to exams whose owning lecturer belongs to the given department.

    The `exams` table has no department column — the JOIN through `lecturers`
    is the only way to apply this filter correctly.

    Intended for the student portal: shows only exams a student is eligible to sit.
    """
    sql = """
        SELECT e.*
        FROM exams e
        JOIN lecturers l ON e.lecturer_id = l.id
        WHERE l.department = ?
          AND e.status = 'Published'
          AND e.chief_approved = 1
        ORDER BY e.created_at DESC
    """
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (department,)).fetchall()
    return _rows_to_dicts(rows)
 
 
def get_exams_pending_approval(department: str) -> list[dict]:
    """
    Returns all exams that are Published but not yet chief-approved,
    belonging to lecturers in the given department.
 
    Used exclusively by pages/chief_examiner/approval.py.
    """
    sql = """
        SELECT  e.id,
                e.course_code,
                e.title,
                e.status,
                e.session_code,
                e.chief_approved,
                e.created_at,
                l.name  AS lecturer_name
        FROM    exams    e
        JOIN    lecturers l ON l.id = e.lecturer_id
        WHERE   e.status         = 'Published'
        AND     e.chief_approved = FALSE
        AND     l.department     = ?
        ORDER BY e.created_at DESC
    """
    with get_connection() as conn:
        conn.row_factory = __import__("sqlite3").Row
        rows = conn.execute(sql, (department,)).fetchall()
    return [dict(row) for row in rows]
 
 
def get_approved_exams_for_student(student_id: str) -> list[dict]:
    """
    Returns all exams that are chief-approved (status='Closed',
    chief_approved=TRUE) for which this student has a submitted session.
 
    Used exclusively by pages/student/results.py.
    """
    sql = """
        SELECT  e.id,
                e.course_code,
                e.title,
                e.session_code,
                e.approved_at
        FROM    exams        e
        JOIN    exam_sessions es ON es.exam_id = e.id
        WHERE   es.student_id  = ?
        AND     es.status      IN ('Submitted', 'Auto-Submitted')
        AND     e.chief_approved = TRUE
        AND     e.status       = 'Closed'
        ORDER BY e.approved_at DESC
    """
    with get_connection() as conn:
        conn.row_factory = __import__("sqlite3").Row
        rows = conn.execute(sql, (student_id,)).fetchall()
    return [dict(row) for row in rows]

def update_exam_status(exam_id: int, status: str) -> None:
    """
    Update the status of an exam.

    Raises ValueError immediately for invalid status values rather than
    letting the DB CHECK constraint produce a less descriptive error.
    """
    if status not in _VALID_EXAM_STATUSES:
        raise ValueError(
            f"Invalid exam status '{status}'. Must be one of: {_VALID_EXAM_STATUSES}."
        )
    sql = "UPDATE exams SET status = ? WHERE id = ?"
    with get_connection() as conn:
        conn.execute(sql, (status, exam_id))


def approve_exam(exam_id: int, approver_id: int) -> None:
    """
    Mark an exam as Chief Examiner-approved.

    Sets chief_approved=TRUE, records approver_id and the current timestamp.
    Does NOT change exam status — use update_exam_status() separately to
    move the exam to 'Closed' after approval if required.
    """
    sql = """
        UPDATE exams
        SET chief_approved = 1,
            approved_by    = ?,
            approved_at    = ?
        WHERE id = ?
    """
    approved_at = datetime.now().isoformat(sep=" ", timespec="seconds")
    with get_connection() as conn:
        conn.execute(sql, (approver_id, approved_at, exam_id))


# ---------------------------------------------------------------------------
# Question functions
# ---------------------------------------------------------------------------

def create_question(
    exam_id: int,
    parent_question_id: int | None,
    question_number: str,
    question_text: str,
    model_answer: str,
    rubric: str,
    max_points: int,
    is_required: bool = True,
) -> int:
    """
    INSERT a new question record.

    Pass parent_question_id=None for top-level questions (Q1, Q2, …).
    Pass a valid question id for sub-questions (Q1a, Q1b, …).

    Returns the new question's integer id (lastrowid).
    Raises sqlite3.IntegrityError if exam_id or parent_question_id is invalid.
    """
    sql = """
        INSERT INTO questions
            (exam_id, parent_question_id, question_number, question_text,
             model_answer, rubric, max_points, is_required)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?)
    """
    with get_connection() as conn:
        cursor = conn.execute(
            sql,
            (
                exam_id,
                parent_question_id,
                question_number,
                question_text,
                model_answer,
                rubric,
                max_points,
                1 if is_required else 0,
            ),
        )
        return cursor.lastrowid


def get_questions_by_exam(exam_id: int) -> list[dict]:
    """
    Return all questions for a given exam, ordered by question_number ASC.

    NOTE: question_number is TEXT, so sort order is lexicographic.
    "10" will sort before "2". If this becomes a problem, a separate integer
    sort_order column would be required (schema change, out of scope for Phase 1).
    The UI layer should be aware of this if question numbers exceed single digits.
    """
    sql = """
        SELECT * FROM questions
        WHERE exam_id = ?
        ORDER BY question_number ASC
    """
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, (exam_id,)).fetchall()
    return _rows_to_dicts(rows)


def get_question_by_id(question_id: int) -> dict | None:
    """
    Fetch a single question row by primary key.

    Returns a dict of all columns, or None if not found.
    """
    sql = "SELECT * FROM questions WHERE id = ? LIMIT 1"
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, (question_id,)).fetchone()
    return _row_to_dict(row) if row is not None else None


def delete_question(question_id: int) -> None:
    """
    DELETE a question by primary key.

    Cascade deletion of child sub-questions is handled by the FK ON DELETE CASCADE
    defined in schema.sql — no manual cleanup required here.

    Silently succeeds if question_id does not exist (idempotent).
    """
    sql = "DELETE FROM questions WHERE id = ?"
    with get_connection() as conn:
        conn.execute(sql, (question_id,))
"""
auth/auth.py — UniGrade Authentication & Session Management

Responsibilities:
- bcrypt password hashing and verification
- Lecturer and student login (DB-backed, parameterized queries)
- Streamlit session state initialization and exam session cleanup

No business logic. No Groq calls. No HTML. No global DB connections.
"""

import streamlit as st
import bcrypt

from models.db_manager import get_connection


# ---------------------------------------------------------------------------
# Password Utilities
# ---------------------------------------------------------------------------

def hash_password(password: str) -> bytes:
    """Hash a plaintext password with bcrypt (work factor 12).

    Returns raw bytes suitable for storing in the DB as TEXT via .decode().
    Callers responsible for encoding the result if storing as string.
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12))


def verify_password(password: str, hashed: bytes | str) -> bool:
    """Verify a plaintext password against a bcrypt hash.

    Accepts hashed as either bytes or str (SQLite returns str; we encode it).
    Returns False on any error rather than propagating exceptions.
    """
    try:
        if isinstance(hashed, str):
            hashed = hashed.encode("utf-8")
        return bcrypt.checkpw(password.encode("utf-8"), hashed)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Lecturer Login
# ---------------------------------------------------------------------------

def login_lecturer(staff_id: str, password: str) -> dict | None:
    """Authenticate a lecturer or chief examiner by staff ID and password.

    Returns a dict with keys {id, name, role, department} on success,
    or None if staff_id is unknown or password is wrong.

    Uses a parameterized query — never interpolates user input into SQL.
    """
    query = """
        SELECT id, name, role, department, password_hash
        FROM lecturers
        WHERE staff_id = ?
    """
    with get_connection() as conn:
        conn.row_factory = _dict_factory
        cursor = conn.execute(query, (staff_id,))
        row = cursor.fetchone()

    if row is None:
        return None

    if not verify_password(password, row["password_hash"]):
        return None

    return {
        "id": row["id"],
        "name": row["name"],
        "role": row["role"],
        "department": row["department"],
    }


# ---------------------------------------------------------------------------
# Student Login
# ---------------------------------------------------------------------------

def login_student(matric_no: str) -> dict | None:
    """Authenticate a student by matric number (no password — Phase 1 rule).

    Returns a dict with keys {matric_no, name, department, level} on success,
    or None if the matric number is not registered.
    """
    query = """
        SELECT matric_no, name, department, level
        FROM students
        WHERE matric_no = ?
    """
    with get_connection() as conn:
        conn.row_factory = _dict_factory
        cursor = conn.execute(query, (matric_no,))
        row = cursor.fetchone()

    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Session State Initialization
# ---------------------------------------------------------------------------

def init_session_defaults() -> None:
    """Initialize ALL session state variables defined in CLAUDE.md §5.

    Uses setdefault() exclusively — never overwrites an existing value.
    Must be called at the top of every render path in app.py.
    """
    # §5.1 — Authentication & User Context
    st.session_state.setdefault("logged_in", False)
    st.session_state.setdefault("role", None)           # "Student" | "Lecturer" | "Chief Examiner"
    st.session_state.setdefault("user_id", None)        # matric_no (str) or lecturers.id (int)
    st.session_state.setdefault("user_name", None)
    st.session_state.setdefault("department", None)

    # §5.2 — Exam Session (Student)
    st.session_state.setdefault("active_exam_id", None)
    st.session_state.setdefault("exam_start_time", None)
    st.session_state.setdefault("exam_end_time", None)
    st.session_state.setdefault("exam_answers", {})         # {question_id: html_text}
    st.session_state.setdefault("answered_questions", set()) # set of question_ids
    st.session_state.setdefault("time_remaining", 0)        # seconds, computed via time-delta
    st.session_state.setdefault("timer_expired", False)
    st.session_state.setdefault("last_autosave_time", 0.0)  # time.time() float
    st.session_state.setdefault("autosave_interval", 30)    # seconds
    st.session_state.setdefault("show_submission_summary", False)
    st.session_state.setdefault("current_question_index", 0)

    # Phase 4 — Focus-loss detection (components/timer.py)
    # Tracks wall-clock time of last timer render to detect rerun gaps.
    st.session_state.setdefault("last_timer_render", 0.0)

    # §5.3 — Grading Dashboard (Lecturer)
    st.session_state.setdefault("selected_exam_id", None)
    st.session_state.setdefault("filter_flagged_only", False)
    st.session_state.setdefault("override_mode", False)

    # §5.4 — Chief Examiner Workflow
    st.session_state.setdefault("pending_approvals", [])
    st.session_state.setdefault("audit_view_exam_id", None)


# ---------------------------------------------------------------------------
# Exam Session Cleanup
# ---------------------------------------------------------------------------

def clear_exam_session() -> None:
    """Reset all active exam state after submission or timeout.

    Clears the student-facing exam slice of session state so a subsequent
    exam start begins with a clean slate. Auth state (logged_in, role,
    user_id, etc.) is preserved.

    CLAUDE.md §5.2: 'CLEAR after submission.'
    """
    st.session_state["exam_answers"] = {}
    st.session_state["answered_questions"] = set()
    st.session_state["active_exam_id"] = None
    st.session_state["exam_start_time"] = None
    st.session_state["exam_end_time"] = None
    st.session_state["timer_expired"] = False
    st.session_state["show_submission_summary"] = False
    st.session_state["current_question_index"] = 0
    st.session_state["last_autosave_time"] = 0.0
    st.session_state["time_remaining"] = 0
    st.session_state["last_timer_render"] = 0.0  # Phase 4: reset focus-loss sentinel


# ---------------------------------------------------------------------------
# Internal Helper
# ---------------------------------------------------------------------------

def _dict_factory(cursor, row) -> dict:
    """sqlite3 row_factory that returns rows as dicts keyed by column name."""
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}
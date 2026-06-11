# models/lecturer_repo.py
# UniGrade package — Lecturer repository
#
# Pure data access layer for the `lecturers` table.
# NO business logic, NO bcrypt, NO Groq calls, NO session state.
#
# IMPORTANT — password_hash storage:
#   bcrypt.hashpw() returns bytes. SQLite stores TEXT.
#   The caller (auth/auth.py) is responsible for encoding/decoding:
#     store:    password_hash.decode("utf-8")
#     retrieve: row["password_hash"].encode("utf-8")
#   This repo is agnostic to encoding — it stores and returns whatever
#   the caller provides. Do NOT encode/decode here.

import sqlite3
from models.db_manager import get_connection


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


def create_lecturer(
    staff_id: str,
    name: str,
    department: str,
    email: str,
    course_code: str,
    course_title: str,
    password_hash: str,
    role: str = "Lecturer",
) -> int:
    """
    INSERT a new lecturer record.

    Returns the new row's integer id (lastrowid).

    Raises sqlite3.IntegrityError if staff_id or email already exists.
    Let this propagate — the caller must handle duplicate registration.

    The `role` value must be 'Lecturer' or 'Chief Examiner'; the DB CHECK
    constraint enforces this at the database level.
    """
    sql = """
        INSERT INTO lecturers
            (staff_id, name, department, email, course_code, course_title, password_hash, role)
        VALUES
            (?, ?, ?, ?, ?, ?, ?, ?)
    """
    with get_connection() as conn:
        cursor = conn.execute(
            sql,
            (staff_id, name, department, email, course_code, course_title, password_hash, role),
        )
        return cursor.lastrowid


def get_by_staff_id(staff_id: str) -> dict | None:
    """
    Fetch a single lecturer row by staff_id.

    Returns a dict of all columns, or None if not found.
    """
    sql = "SELECT * FROM lecturers WHERE staff_id = ? LIMIT 1"
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, (staff_id,)).fetchone()
    return _row_to_dict(row) if row is not None else None


def get_by_id(lecturer_id: int) -> dict | None:
    """
    Fetch a single lecturer row by primary key id.

    Returns a dict of all columns, or None if not found.
    """
    sql = "SELECT * FROM lecturers WHERE id = ? LIMIT 1"
    with get_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, (lecturer_id,)).fetchone()
    return _row_to_dict(row) if row is not None else None


def email_exists(email: str) -> bool:
    """
    Return True if the given email is already registered, False otherwise.

    Use before INSERT to give a friendlier error than an IntegrityError.
    """
    sql = "SELECT 1 FROM lecturers WHERE email = ? LIMIT 1"
    with get_connection() as conn:
        row = conn.execute(sql, (email,)).fetchone()
    return row is not None


def staff_id_exists(staff_id: str) -> bool:
    """
    Return True if the given staff_id is already registered, False otherwise.

    Use before INSERT to give a friendlier error than an IntegrityError.
    """
    sql = "SELECT 1 FROM lecturers WHERE staff_id = ? LIMIT 1"
    with get_connection() as conn:
        row = conn.execute(sql, (staff_id,)).fetchone()
    return row is not None
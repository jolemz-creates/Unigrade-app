# tests/test_database.py
# UniGrade package — Database schema and constraint tests
#
# Tests in this file operate at the schema level using raw SQL.
# They do NOT test repo functions — those have their own test files.
# Using raw SQL here means a bug in a repo function cannot cause a
# schema test to fail or pass incorrectly.
#
# NOTE: The Phase 1C spec mentions "6 tables" but the schema defines 7:
# lecturers, exams, questions, students, student_responses, exam_sessions,
# audit_log. All 7 are asserted in test_init_db_creates_all_tables().

import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


class TestDatabaseSchema(unittest.TestCase):
    """
    Each test gets a completely fresh SQLite database via setUp/tearDown.
    This prevents any test from bleeding state into another.

    All connections are opened via get_connection() (imported after patching
    DB_PATH) so that PRAGMA foreign_keys=ON is always active.
    """

    def setUp(self):
        # Create a temp file for the isolated test DB.
        # Close the OS-level fd immediately — SQLite opens the file itself,
        # and an open fd causes access errors on some platforms.
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)

        # Patch DB_PATH before importing anything that calls get_connection().
        self.path_patcher = patch("models.db_manager.DB_PATH", self.db_path)
        self.path_patcher.start()

        # init_db() must be imported AFTER patching so it uses the temp path.
        from models.db_manager import init_db
        init_db()

    def tearDown(self):
        self.path_patcher.stop()
        # Remove temp DB file. Ignore errors if the file is already gone.
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    def _seed_base_data(self, conn: sqlite3.Connection) -> None:
        """
        Insert the minimum viable data chain required by FK-dependent tests:
            lecturer → exam → question → student

        Does NOT insert student_responses or exam_sessions — tests that need
        those rows create them explicitly so assertions stay readable.
        """
        conn.execute("""
            INSERT INTO lecturers
                (staff_id, name, department, email, course_code, course_title, password_hash, role)
            VALUES
                ('S001', 'Dr. Test', 'Computer Science', 'test@unilorin.edu.ng',
                 'CSC201', 'Data Structures', 'hashed_pw', 'Lecturer')
        """)
        conn.execute("""
            INSERT INTO exams
                (lecturer_id, course_code, title, instructions, duration, session_code, status)
            VALUES
                (1, 'CSC201', 'Midterm Exam', '', 60, '2024-CSC201-MIDTER', 'Draft')
        """)
        conn.execute("""
            INSERT INTO questions
                (exam_id, parent_question_id, question_number, question_text,
                 model_answer, rubric, max_points, is_required)
            VALUES
                (1, NULL, '1', 'Define OOP.',
                 'OOP is a paradigm based on objects.', '5 marks for correct definition', 5, 1)
        """)
        conn.execute("""
            INSERT INTO students (matric_no, name, department, level, email)
            VALUES ('21/52CS001', 'John Doe', 'Computer Science', 200, 'john@student.unilorin.edu.ng')
        """)

    # ------------------------------------------------------------------
    # Test 1 — init_db() creates all tables
    # ------------------------------------------------------------------

    def test_init_db_creates_all_tables(self):
        """All 7 tables defined in the schema must exist after init_db()."""
        expected_tables = {
            "lecturers",
            "exams",
            "questions",
            "students",
            "student_responses",
            "exam_sessions",
            "audit_log",
        }
        from models.db_manager import get_connection

        with get_connection() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()

        actual_tables = {row[0] for row in rows}
        self.assertTrue(
            expected_tables.issubset(actual_tables),
            f"Missing tables: {expected_tables - actual_tables}",
        )

    # ------------------------------------------------------------------
    # Test 2 — WAL mode is active after init_db()
    # ------------------------------------------------------------------

    def test_wal_mode_is_active(self):
        """PRAGMA journal_mode must return 'wal' on a fresh connection."""
        from models.db_manager import get_connection

        with get_connection() as conn:
            result = conn.execute("PRAGMA journal_mode").fetchone()

        self.assertEqual(
            result[0].lower(),
            "wal",
            "Expected WAL journal mode to be active after init_db().",
        )

    # ------------------------------------------------------------------
    # Test 3 — Foreign keys are ON after every connection
    # ------------------------------------------------------------------

    def test_foreign_keys_are_on(self):
        """PRAGMA foreign_keys must return 1 on every get_connection() call."""
        from models.db_manager import get_connection

        with get_connection() as conn:
            result = conn.execute("PRAGMA foreign_keys").fetchone()

        self.assertEqual(
            result[0],
            1,
            "Expected PRAGMA foreign_keys=ON (1) on every connection.",
        )

    # ------------------------------------------------------------------
    # Test 4 — FK violation on student_responses raises IntegrityError
    # ------------------------------------------------------------------

    def test_student_response_invalid_exam_id_raises(self):
        """
        Inserting a student_response referencing a non-existent exam_id must
        raise IntegrityError when foreign_keys=ON.
        """
        from models.db_manager import get_connection

        with self.assertRaises(sqlite3.IntegrityError):
            with get_connection() as conn:
                # exam_id=9999 does not exist — FK violation.
                conn.execute("""
                    INSERT INTO students (matric_no, name, department, level)
                    VALUES ('21/52CS001', 'John Doe', 'Computer Science', 200)
                """)
                conn.execute("""
                    INSERT INTO student_responses
                        (exam_id, student_id, question_id, answer_text, sanitized_text)
                    VALUES
                        (9999, '21/52CS001', 1, '<p>Answer</p>', 'Answer')
                """)

    # ------------------------------------------------------------------
    # Test 5 — Deleting an exam cascades to its questions
    # ------------------------------------------------------------------

    def test_delete_exam_cascades_to_questions(self):
        """
        Deleting an exam must cascade-delete all linked question rows.
        """
        from models.db_manager import get_connection

        with get_connection() as conn:
            self._seed_base_data(conn)

        # Verify the question exists before the cascade.
        with get_connection() as conn:
            count_before = conn.execute(
                "SELECT COUNT(*) FROM questions WHERE exam_id = 1"
            ).fetchone()[0]
        self.assertEqual(count_before, 1, "Seed data should have inserted 1 question.")

        # Delete the exam — cascade should remove the question.
        with get_connection() as conn:
            conn.execute("DELETE FROM exams WHERE id = 1")

        with get_connection() as conn:
            count_after = conn.execute(
                "SELECT COUNT(*) FROM questions WHERE exam_id = 1"
            ).fetchone()[0]
        self.assertEqual(
            count_after,
            0,
            "Deleting an exam must cascade-delete its questions (ON DELETE CASCADE).",
        )

    # ------------------------------------------------------------------
    # Test 6 — Deleting an exam cascades to student_responses
    # ------------------------------------------------------------------

    def test_delete_exam_cascades_to_student_responses(self):
        """
        Deleting an exam must cascade-delete all linked student_response rows.
        """
        from models.db_manager import get_connection

        # Seed base data and a student_response.
        with get_connection() as conn:
            self._seed_base_data(conn)
            conn.execute("""
                INSERT INTO student_responses
                    (exam_id, student_id, question_id, answer_text, sanitized_text, submitted_at)
                VALUES
                    (1, '21/52CS001', 1, '<p>OOP is...</p>', 'OOP is...', '2024-01-15 10:00:00')
            """)

        # Verify the response exists before cascade.
        with get_connection() as conn:
            count_before = conn.execute(
                "SELECT COUNT(*) FROM student_responses WHERE exam_id = 1"
            ).fetchone()[0]
        self.assertEqual(count_before, 1, "Seed data should have inserted 1 student_response.")

        # Delete the exam.
        with get_connection() as conn:
            conn.execute("DELETE FROM exams WHERE id = 1")

        with get_connection() as conn:
            count_after = conn.execute(
                "SELECT COUNT(*) FROM student_responses WHERE exam_id = 1"
            ).fetchone()[0]
        self.assertEqual(
            count_after,
            0,
            "Deleting an exam must cascade-delete its student_responses (ON DELETE CASCADE).",
        )

    # ------------------------------------------------------------------
    # Test 7 — UNIQUE constraint on student_responses
    # ------------------------------------------------------------------

    def test_unique_constraint_on_student_responses(self):
        """
        Inserting two student_response rows with the same (exam_id, student_id,
        question_id) must raise IntegrityError.
        """
        from models.db_manager import get_connection

        with get_connection() as conn:
            self._seed_base_data(conn)
            # First insert — must succeed.
            conn.execute("""
                INSERT INTO student_responses
                    (exam_id, student_id, question_id, answer_text, sanitized_text, submitted_at)
                VALUES
                    (1, '21/52CS001', 1, '<p>First answer</p>', 'First answer', '2024-01-15 10:00:00')
            """)

        # Second insert with the same natural key — must fail.
        with self.assertRaises(sqlite3.IntegrityError):
            with get_connection() as conn:
                conn.execute("""
                    INSERT INTO student_responses
                        (exam_id, student_id, question_id, answer_text, sanitized_text, submitted_at)
                    VALUES
                        (1, '21/52CS001', 1, '<p>Duplicate answer</p>', 'Duplicate answer', '2024-01-15 10:05:00')
                """)

    # ------------------------------------------------------------------
    # Test 8 — UNIQUE constraint on exam_sessions
    # ------------------------------------------------------------------

    def test_unique_constraint_on_exam_sessions(self):
        """
        Inserting two exam_session rows for the same (exam_id, student_id)
        must raise IntegrityError.
        """
        from models.db_manager import get_connection

        with get_connection() as conn:
            self._seed_base_data(conn)
            # First session — must succeed.
            conn.execute("""
                INSERT INTO exam_sessions
                    (exam_id, student_id, start_time, end_time, status, ip_address)
                VALUES
                    (1, '21/52CS001', '2024-01-15 09:00:00', '2024-01-15 10:00:00',
                     'In Progress', '192.168.1.1')
            """)

        # Second session for the same student in the same exam — must fail.
        with self.assertRaises(sqlite3.IntegrityError):
            with get_connection() as conn:
                conn.execute("""
                    INSERT INTO exam_sessions
                        (exam_id, student_id, start_time, end_time, status, ip_address)
                    VALUES
                        (1, '21/52CS001', '2024-01-15 09:05:00', '2024-01-15 10:05:00',
                         'In Progress', '192.168.1.2')
                """)


if __name__ == "__main__":
    unittest.main()
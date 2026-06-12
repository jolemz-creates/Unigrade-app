# tests/test_concurrency.py
# UniGrade package — WAL concurrency stress tests (Phase 4)
#
# Validates that the SQLite WAL + busy_timeout configuration in db_manager.py
# allows 50 concurrent writer threads to complete without any OperationalError
# ("database is locked") collisions.
#
# WHY THIS MATTERS
# ----------------
# SQLite in default journal mode uses exclusive write locks — a second writer
# hitting the lock with busy_timeout=0 (the default) raises OperationalError
# immediately. WAL mode improves read/write concurrency but still serialises
# writers. The busy_timeout PRAGMA (set to 5000 ms in db_manager.py) tells
# SQLite to retry rather than fail immediately, which is what prevents lock
# errors under peak exam-submission load (hundreds of students submitting
# simultaneously).
#
# WHAT IS TESTED
# --------------
# 1. 50 concurrent threads each insert one student_response row.
#    Each thread gets its own connection (via get_connection()) — this is the
#    real-world pattern since Streamlit spawns a thread per user session.
#    Assertion: zero OperationalError exceptions across all 50 threads.
#
# 2. All 50 rows are committed and readable after all threads complete.
#    Assertion: COUNT(*) == 50. This catches silent rollbacks or lost writes.
#
# 3. The UNIQUE constraint on student_responses(exam_id, student_id, question_id)
#    is preserved under concurrency — two threads racing to insert the same key
#    must not corrupt the database; one succeeds, one gets IntegrityError (not
#    OperationalError), and the DB remains consistent.
#    Assertion: COUNT(*) == 1 after the race; no OperationalError raised.

import os
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch


# Number of concurrent writer threads for the main stress test.
THREAD_COUNT = 50


class TestWALConcurrency(unittest.TestCase):
    """
    Each test method gets a completely isolated temp database via setUp/tearDown.
    The DB_PATH patch is applied before init_db() so all threads in the test
    write to the temp file, not the real unigrade.db.
    """

    def setUp(self):
        # Temp file for the isolated test DB.
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)

        # Patch DB_PATH before importing anything that calls get_connection().
        self.path_patcher = patch("models.db_manager.DB_PATH", self.db_path)
        self.path_patcher.start()

        from models.db_manager import init_db
        init_db()

        # Seed the FK dependency chain required by student_responses:
        #   lecturer → exam → question → (students seeded per-thread)
        # We seed students here too (one per thread) to avoid FK violations
        # inside the threads themselves. Students use matric format TT/00CS<N>.
        from models.db_manager import get_connection
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO lecturers
                    (staff_id, name, department, email, course_code,
                     course_title, password_hash, role)
                VALUES
                    ('CONC001', 'Dr. Concurrent', 'Computer Science',
                     'conc@unilorin.edu.ng', 'CSC999', 'Concurrency 101',
                     'hashed_pw', 'Lecturer')
            """)
            conn.execute("""
                INSERT INTO exams
                    (lecturer_id, course_code, title, instructions,
                     duration, session_code, status)
                VALUES
                    (1, 'CSC999', 'Stress Test Exam', '', 60,
                     '2024-CSC999-STRESS', 'Published')
            """)
            conn.execute("""
                INSERT INTO questions
                    (exam_id, parent_question_id, question_number, question_text,
                     model_answer, rubric, max_points, is_required)
                VALUES
                    (1, NULL, '1', 'What is concurrency?',
                     'Concurrent execution of tasks.', '5 marks', 5, 1)
            """)
            # Pre-seed one student per thread so FK checks pass inside threads.
            for i in range(THREAD_COUNT):
                matric = _matric(i)
                conn.execute("""
                    INSERT INTO students (matric_no, name, department, level)
                    VALUES (?, ?, 'Computer Science', 200)
                """, (matric, f"Student {i}"))

    def tearDown(self):
        self.path_patcher.stop()
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Test 1 — 50 concurrent writers, zero OperationalError
    # ------------------------------------------------------------------

    def test_50_concurrent_writers_no_lock_errors(self):
        """
        50 threads each inserting a distinct student_response must complete
        without any OperationalError. All 50 rows must be present afterwards.

        This is the primary WAL stress test. It fails without busy_timeout
        set in get_connection() because SQLite serialises WAL writers and
        threads that arrive while the write lock is held will immediately
        raise OperationalError with busy_timeout=0 (the SQLite default).
        """
        errors: list[Exception] = []
        lock = threading.Lock()

        def insert_response(thread_index: int) -> None:
            from models.db_manager import get_connection
            matric = _matric(thread_index)
            try:
                with get_connection() as conn:
                    conn.execute("""
                        INSERT INTO student_responses
                            (exam_id, student_id, question_id,
                             answer_text, sanitized_text, submitted_at)
                        VALUES
                            (1, ?, 1, '<p>Answer</p>', 'Answer',
                             '2024-06-01 10:00:00')
                    """, (matric,))
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=insert_response, args=(i,))
            for i in range(THREAD_COUNT)
        ]

        # Start all threads as close together as possible.
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Assert zero errors of any kind — specifically no OperationalError.
        operational_errors = [
            e for e in errors if isinstance(e, sqlite3.OperationalError)
        ]
        other_errors = [
            e for e in errors if not isinstance(e, sqlite3.OperationalError)
        ]

        self.assertEqual(
            len(operational_errors),
            0,
            f"Got {len(operational_errors)} OperationalError(s) under concurrent "
            f"writes — WAL + busy_timeout not working correctly.\n"
            f"Errors: {operational_errors}",
        )
        self.assertEqual(
            len(other_errors),
            0,
            f"Got {len(other_errors)} unexpected error(s) during concurrent writes.\n"
            f"Errors: {other_errors}",
        )

        # Verify all rows landed.
        from models.db_manager import get_connection
        with get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM student_responses WHERE exam_id = 1"
            ).fetchone()[0]

        self.assertEqual(
            count,
            THREAD_COUNT,
            f"Expected {THREAD_COUNT} rows after concurrent inserts, got {count}. "
            "Some writes were silently lost.",
        )

    # ------------------------------------------------------------------
    # Test 2 — Concurrent writes preserve UNIQUE constraint integrity
    # ------------------------------------------------------------------

    def test_concurrent_duplicate_insert_raises_integrity_not_operational(self):
        """
        Two threads racing to insert the SAME (exam_id, student_id, question_id)
        must produce at most one IntegrityError — never an OperationalError.

        This verifies that constraint violations under concurrency fail cleanly
        (IntegrityError) rather than corrupting the database or masking the
        collision as a lock error.
        """
        # Both threads target student 0's matric — same natural key.
        matric = _matric(0)
        errors: list[Exception] = []
        lock = threading.Lock()

        def insert_duplicate(_: int) -> None:
            from models.db_manager import get_connection
            try:
                with get_connection() as conn:
                    conn.execute("""
                        INSERT INTO student_responses
                            (exam_id, student_id, question_id,
                             answer_text, sanitized_text, submitted_at)
                        VALUES
                            (1, ?, 1, '<p>Race answer</p>', 'Race answer',
                             '2024-06-01 10:00:00')
                    """, (matric,))
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [threading.Thread(target=insert_duplicate, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly one thread must have succeeded (zero errors) or one got
        # IntegrityError. Either way: zero OperationalErrors.
        operational_errors = [
            e for e in errors if isinstance(e, sqlite3.OperationalError)
        ]
        integrity_errors = [
            e for e in errors if isinstance(e, sqlite3.IntegrityError)
        ]

        self.assertEqual(
            len(operational_errors),
            0,
            f"UNIQUE conflict produced OperationalError instead of IntegrityError: "
            f"{operational_errors}",
        )

        # One thread succeeded, one got IntegrityError — or both succeeded if
        # SQLite serialised them perfectly (unlikely but valid). Either way,
        # the row count must be exactly 1.
        self.assertLessEqual(
            len(integrity_errors),
            1,
            f"Expected at most 1 IntegrityError from duplicate race, got "
            f"{len(integrity_errors)}.",
        )

        from models.db_manager import get_connection
        with get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM student_responses "
                "WHERE exam_id = 1 AND student_id = ?",
                (matric,),
            ).fetchone()[0]

        self.assertEqual(
            count,
            1,
            f"Expected exactly 1 row after duplicate race, got {count}. "
            "Database may have accepted a duplicate or lost both writes.",
        )

    # ------------------------------------------------------------------
    # Test 3 — busy_timeout is set on every connection
    # ------------------------------------------------------------------

    def test_busy_timeout_pragma_is_set(self):
        """
        PRAGMA busy_timeout must return a non-zero value (we set 5000 ms)
        on every connection opened via get_connection().

        This is a fast unit-level check that the PRAGMA is actually being
        applied, without needing to race threads.
        """
        from models.db_manager import get_connection

        with get_connection() as conn:
            timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

        self.assertGreater(
            timeout,
            0,
            "PRAGMA busy_timeout should be > 0 (set to 5000 in db_manager.py). "
            "Without this, concurrent writes will raise OperationalError immediately.",
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _matric(index: int) -> str:
    """Generate a deterministic matric number for thread `index`.

    Format: TT/00CS<NNN> — 'TT' prefix flags these as test records.
    Zero-padded to 3 digits so natural sort order matches thread index.
    """
    return f"TT/00CS{index:03d}"


if __name__ == "__main__":
    unittest.main()
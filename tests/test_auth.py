# tests/test_auth.py
# UniGrade package — Auth system unit tests
#
# These tests are written TDD-style: auth/auth.py does not yet exist (Phase 2A).
# All tests will fail with ImportError until auth.py is implemented.
# That is correct and expected.
#
# Test classes:
#   TestPasswordHashing  — hash_password / verify_password (no DB, no Streamlit)
#   TestAuthLogin        — login_lecturer / login_student (isolated test DB)
#   TestSessionDefaults  — init_session_defaults (mocked st.session_state)

import os
import tempfile
import unittest
from unittest.mock import patch

import bcrypt


# ---------------------------------------------------------------------------
# TestPasswordHashing — pure crypto, no external dependencies
# ---------------------------------------------------------------------------

class TestPasswordHashing(unittest.TestCase):
    """
    Tests for hash_password() and verify_password().
    No database or Streamlit involvement.

    bcrypt is called with rounds=12 inside auth.py (per CLAUDE.md §8.4).
    These tests do not control the work factor — they assert on observable
    behaviour (type, correctness, rejection) rather than internals.
    """

    def test_hash_password_returns_bytes(self):
        """hash_password must return bytes, not a str or any other type."""
        from auth.auth import hash_password

        result = hash_password("testpass123")
        self.assertIsInstance(
            result,
            bytes,
            "hash_password() must return bytes (bcrypt.hashpw output).",
        )

    def test_hash_password_is_not_plaintext(self):
        """The returned bytes must not be the UTF-8 encoding of the input."""
        from auth.auth import hash_password

        password = "testpass123"
        result = hash_password(password)
        self.assertNotEqual(
            result,
            password.encode("utf-8"),
            "hash_password() must not return plaintext bytes.",
        )

    def test_verify_password_correct_password_returns_true(self):
        """verify_password must return True when the password matches the hash."""
        from auth.auth import hash_password, verify_password

        password = "CorrectHorseBatteryStaple"
        hashed = hash_password(password)
        self.assertTrue(
            verify_password(password, hashed),
            "verify_password() must return True for the correct password.",
        )

    def test_verify_password_wrong_password_returns_false(self):
        """verify_password must return False when the password does not match."""
        from auth.auth import hash_password, verify_password

        hashed = hash_password("CorrectHorseBatteryStaple")
        self.assertFalse(
            verify_password("WrongPassword!", hashed),
            "verify_password() must return False for an incorrect password.",
        )


# ---------------------------------------------------------------------------
# TestAuthLogin — DB-dependent login tests
# ---------------------------------------------------------------------------

class TestAuthLogin(unittest.TestCase):
    """
    Tests for login_lecturer() and login_student().

    Uses an isolated temporary SQLite database per test run.
    bcrypt hashes are created directly (not via hash_password) so setUp
    does not depend on auth.py being implemented — pure TDD.

    NOTE: rounds=4 is used for bcrypt in setUp to keep tests fast.
    Production code in auth.py uses rounds=12 as mandated by CLAUDE.md §8.4.
    """

    # Password used for the seeded test lecturer.
    _TEST_PASSWORD = "SecureTestPass123!"
    # Wrong password used in negative tests.
    _WRONG_PASSWORD = "NotTheRightPassword"

    def setUp(self):
        # Fresh isolated DB for every test.
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)

        self.path_patcher = patch("models.db_manager.DB_PATH", self.db_path)
        self.path_patcher.start()

        from models.db_manager import get_connection, init_db
        init_db()

        # Hash the test password with rounds=4 (speed). Store as TEXT.
        hashed_bytes = bcrypt.hashpw(
            self._TEST_PASSWORD.encode("utf-8"),
            bcrypt.gensalt(rounds=4),
        )
        stored_hash = hashed_bytes.decode("utf-8")

        with get_connection() as conn:
            conn.execute(
                """
                INSERT INTO lecturers
                    (staff_id, name, department, email,
                     course_code, course_title, password_hash, role)
                VALUES
                    ('S001', 'Dr. Test Lecturer', 'Computer Science',
                     'test@unilorin.edu.ng', 'CSC201', 'Data Structures',
                     ?, 'Lecturer')
                """,
                (stored_hash,),
            )
            conn.execute(
                """
                INSERT INTO students (matric_no, name, department, level, email)
                VALUES ('21/52CS001', 'John Doe', 'Computer Science', 200,
                        'john@student.unilorin.edu.ng')
                """,
            )

    def tearDown(self):
        self.path_patcher.stop()
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

    # --- login_lecturer ---

    def test_login_lecturer_unknown_staff_id_returns_none(self):
        """login_lecturer must return None for a staff_id not in the database."""
        from auth.auth import login_lecturer

        result = login_lecturer("UNKNOWN_ID", "anypassword")
        self.assertIsNone(
            result,
            "login_lecturer() must return None for an unrecognised staff_id.",
        )

    def test_login_lecturer_wrong_password_returns_none(self):
        """login_lecturer must return None when staff_id is valid but password is wrong."""
        from auth.auth import login_lecturer

        result = login_lecturer("S001", self._WRONG_PASSWORD)
        self.assertIsNone(
            result,
            "login_lecturer() must return None for a correct staff_id but wrong password.",
        )

    def test_login_lecturer_valid_credentials_returns_dict(self):
        """login_lecturer must return a dict with id, name, role, department on success."""
        from auth.auth import login_lecturer

        result = login_lecturer("S001", self._TEST_PASSWORD)

        self.assertIsNotNone(result, "login_lecturer() must return a dict on valid credentials.")
        self.assertIn("id", result, "Return dict must include 'id'.")
        self.assertIn("name", result, "Return dict must include 'name'.")
        self.assertIn("role", result, "Return dict must include 'role'.")
        self.assertIn("department", result, "Return dict must include 'department'.")
        self.assertEqual(result["name"], "Dr. Test Lecturer")
        self.assertEqual(result["role"], "Lecturer")
        self.assertEqual(result["department"], "Computer Science")

    # --- login_student ---

    def test_login_student_unknown_matric_returns_none(self):
        """login_student must return None for a matric number not in the database."""
        from auth.auth import login_student

        result = login_student("99/99XX999")
        self.assertIsNone(
            result,
            "login_student() must return None for an unrecognised matric number.",
        )

    def test_login_student_known_matric_returns_dict(self):
        """login_student must return a student dict for a known matric number."""
        from auth.auth import login_student

        result = login_student("21/52CS001")

        self.assertIsNotNone(result, "login_student() must return a dict for a known matric_no.")
        self.assertEqual(result["matric_no"], "21/52CS001")
        self.assertEqual(result["name"], "John Doe")
        self.assertEqual(result["department"], "Computer Science")


# ---------------------------------------------------------------------------
# TestSessionDefaults — mocked st.session_state
# ---------------------------------------------------------------------------

class TestSessionDefaults(unittest.TestCase):
    """
    Tests for init_session_defaults().

    Streamlit is not running in test context, so st.session_state is mocked
    as a plain dict. dict.setdefault() has exactly the semantics auth.py
    is required to use (CLAUDE.md §5 Initialization Rule), so no additional
    mock configuration is needed.
    """

    # Complete list of session state keys from CLAUDE.md §5.1 – §5.4.
    # If new keys are added to auth.py, add them here too.
    EXPECTED_KEYS = [
        # §5.1 Auth & User Context
        "logged_in",
        "role",
        "user_id",
        "user_name",
        "department",
        # §5.2 Exam Session (Student)
        "active_exam_id",
        "exam_start_time",
        "exam_end_time",
        "exam_answers",
        "answered_questions",
        "time_remaining",
        "timer_expired",
        "last_autosave_time",
        "autosave_interval",
        "show_submission_summary",
        "current_question_index",
        # §5.3 Grading Dashboard (Lecturer)
        "selected_exam_id",
        "filter_flagged_only",
        "override_mode",
        # §5.4 Chief Examiner Workflow
        "pending_approvals",
        "audit_view_exam_id",
    ]

    def test_init_session_defaults_sets_all_expected_keys(self):
        """
        init_session_defaults() must initialise every session state key
        listed in CLAUDE.md §5 using setdefault().
        """
        from auth.auth import init_session_defaults

        mock_state = {}
        with patch("auth.auth.st") as mock_st:
            mock_st.session_state = mock_state
            init_session_defaults()

        missing = [key for key in self.EXPECTED_KEYS if key not in mock_state]
        self.assertFalse(
            missing,
            f"init_session_defaults() did not initialise these keys: {missing}",
        )

    def test_init_session_defaults_does_not_overwrite_existing_values(self):
        """
        init_session_defaults() must use setdefault() semantics: keys that
        already have a value in session_state must not be overwritten.
        """
        from auth.auth import init_session_defaults

        # Pre-populate a few keys with non-default truthy values.
        pre_existing = {
            "logged_in": True,
            "role": "Chief Examiner",
            "user_name": "Prof. Already Set",
        }
        mock_state = dict(pre_existing)

        with patch("auth.auth.st") as mock_st:
            mock_st.session_state = mock_state
            init_session_defaults()

        for key, expected_value in pre_existing.items():
            self.assertEqual(
                mock_state[key],
                expected_value,
                f"init_session_defaults() overwrote pre-existing value for '{key}'.",
            )


if __name__ == "__main__":
    unittest.main()
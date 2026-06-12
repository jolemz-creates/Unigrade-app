# tests/test_grader_retry_additions.py
# Phase 4 — Rate-limit guard (exponential backoff retry) tests
#
# APPEND THIS CLASS TO tests/test_grader.py in your project.
# Kept standalone here for independent execution.
#
# Load strategy: grader.py is loaded directly from disk via importlib.util
# so it bypasses the services/ package system entirely. External deps
# (groq, dotenv, services.audit, services.sanitizer) are stubbed in
# sys.modules before the load, then cleaned up after each test class.

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Path to the actual grader.py on disk — relative to this test file.
_GRADER_PATH = Path(__file__).parent.parent / "services" / "grader.py"


def _load_grader_with_stubs():
    """
    Load services/grader.py with all external dependencies stubbed.
    Returns the loaded module object.
    """
    # Build dependency stubs.
    groq_stub = types.ModuleType("groq")
    groq_stub.Groq = MagicMock()
    groq_stub.AsyncGroq = MagicMock()

    dotenv_stub = types.ModuleType("dotenv")
    dotenv_stub.load_dotenv = lambda: None

    audit_stub = types.ModuleType("services.audit")
    audit_stub.log_to_audit = MagicMock()

    san_stub = types.ModuleType("services.sanitizer")
    san_stub.strip_html_tags = lambda x: x

    stubs = {
        "groq": groq_stub,
        "dotenv": dotenv_stub,
        "services.audit": audit_stub,
        "services.sanitizer": san_stub,
    }

    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location("services.grader", _GRADER_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules["services.grader"] = module
        spec.loader.exec_module(module)

    return module


class TestGroqRetryBehaviour(unittest.TestCase):
    """
    Verify exponential backoff retry logic in grader.py under simulated
    API failures. No real Groq API key or network access required.
    time.sleep() is patched per-test so the suite runs instantly.
    """

    @classmethod
    def setUpClass(cls):
        cls.grader = _load_grader_with_stubs()

    # ------------------------------------------------------------------
    # Test 1 — First attempt succeeds: no retry, no sleep
    # ------------------------------------------------------------------

    def test_sync_retry_succeeds_on_first_attempt(self):
        """First call succeeds → no sleep, no audit log entry."""
        mock_response = MagicMock()
        mock_response.choices[0].message.content = (
            '{"score": 4.0, "feedback": "Good.", "confidence": 0.9}'
        )
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = mock_response

        with patch.object(self.grader, "_get_groq_client", return_value=mock_client), \
             patch.object(self.grader.time, "sleep") as mock_sleep, \
             patch.object(self.grader, "log_to_audit") as mock_audit:

            result = self.grader._call_groq_sync_with_retry(
                "prompt text", exam_id=1, question_id=1
            )

        self.assertIsNotNone(result)
        self.assertIn('"score": 4.0', result)
        mock_sleep.assert_not_called()
        mock_audit.assert_not_called()

    # ------------------------------------------------------------------
    # Test 2 — Fails once, succeeds on second attempt
    # ------------------------------------------------------------------

    def test_sync_retry_succeeds_on_second_attempt(self):
        """First fails, second succeeds → sleep(2), one audit log entry."""
        mock_response = MagicMock()
        mock_response.choices[0].message.content = (
            '{"score": 3.0, "feedback": "OK.", "confidence": 0.8}'
        )
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            Exception("429 rate limit"),
            mock_response,
        ]

        with patch.object(self.grader, "_get_groq_client", return_value=mock_client), \
             patch.object(self.grader.time, "sleep") as mock_sleep, \
             patch.object(self.grader, "log_to_audit") as mock_audit:

            result = self.grader._call_groq_sync_with_retry(
                "prompt text", exam_id=1, question_id=2
            )

        self.assertIsNotNone(result)
        mock_sleep.assert_called_once_with(2)   # 2^1
        self.assertEqual(mock_audit.call_count, 1)
        details = mock_audit.call_args[1]["details"]
        self.assertEqual(details["attempt"], 1)
        self.assertTrue(details["will_retry"])

    # ------------------------------------------------------------------
    # Test 3 — All 3 attempts fail
    # ------------------------------------------------------------------

    def test_sync_retry_all_attempts_exhausted_returns_none(self):
        """All 3 attempts fail → None, sleep(2)+sleep(4), 3 audit entries."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("503")

        with patch.object(self.grader, "_get_groq_client", return_value=mock_client), \
             patch.object(self.grader.time, "sleep") as mock_sleep, \
             patch.object(self.grader, "log_to_audit") as mock_audit:

            result = self.grader._call_groq_sync_with_retry(
                "prompt text", exam_id=2, question_id=3
            )

        self.assertIsNone(result)
        self.assertEqual(mock_sleep.call_count, 2)
        mock_sleep.assert_any_call(2)
        mock_sleep.assert_any_call(4)
        self.assertEqual(mock_audit.call_count, 3)
        last_details = mock_audit.call_args[1]["details"]
        self.assertFalse(last_details["will_retry"])
        self.assertEqual(last_details["attempt"], 3)

    # ------------------------------------------------------------------
    # Test 4 — grade_single_response returns fallback after retry exhaustion
    # ------------------------------------------------------------------

    def test_grade_single_response_returns_fallback_after_retry_exhaustion(self):
        """grade_single_response returns score=0/confidence=0 after all retries fail."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = Exception("timeout")

        with patch.object(self.grader, "_get_groq_client", return_value=mock_client), \
             patch.object(self.grader.time, "sleep"), \
             patch.object(self.grader, "log_to_audit"), \
             patch.object(self.grader, "strip_html_tags", side_effect=lambda x: x):

            result = self.grader.grade_single_response(
                question_id=1,
                question_text="Explain recursion.",
                model_answer="A function calling itself.",
                rubric="5 marks for correct explanation.",
                max_marks=5.0,
                student_answer_html="Recursion is when a function calls itself.",
                exam_id=1,
            )

        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["confidence"], 0.0)
        self.assertIn("manual review", result["feedback"])

    # ------------------------------------------------------------------
    # Test 5 — EnvironmentError (missing API key) is not retried
    # ------------------------------------------------------------------

    def test_missing_api_key_not_retried(self):
        """EnvironmentError from _get_groq_client → fallback, no sleep, one audit entry."""
        with patch.object(
                self.grader, "_get_groq_client",
                side_effect=EnvironmentError("no key")
             ), \
             patch.object(self.grader.time, "sleep") as mock_sleep, \
             patch.object(self.grader, "log_to_audit") as mock_audit, \
             patch.object(self.grader, "strip_html_tags", side_effect=lambda x: x):

            result = self.grader.grade_single_response(
                question_id=1,
                question_text="What is a stack?",
                model_answer="LIFO data structure.",
                rubric="5 marks",
                max_marks=5.0,
                student_answer_html="A stack is LIFO.",
                exam_id=1,
            )

        self.assertEqual(result["score"], 0.0)
        mock_sleep.assert_not_called()
        self.assertEqual(mock_audit.call_count, 1)
        logged_action = (
            mock_audit.call_args[1].get("action")
            or mock_audit.call_args[0][0]
        )
        self.assertIn("configuration", logged_action.lower())

    # ------------------------------------------------------------------
    # Test 6 — Async path: all attempts fail, fallback has correct question_id
    # ------------------------------------------------------------------

    def test_async_retry_all_fail_returns_fallback_with_question_id(self):
        """
        grade_exam_batch() returns fallback with question_id echoed when
        all async retry attempts fail. Must not raise.
        """
        mock_async_instance = MagicMock()
        mock_async_instance.chat.completions.create = AsyncMock(
            side_effect=Exception("async 429")
        )

        # Patch AsyncGroq and os.getenv; keep asyncio real.
        with patch.object(self.grader, "AsyncGroq", return_value=mock_async_instance), \
             patch.object(self.grader.os, "getenv", return_value="fake-key"), \
             patch.object(self.grader, "log_to_audit"), \
             patch.object(self.grader, "strip_html_tags", side_effect=lambda x: x):

            # asyncio.sleep inside _call_groq_async_with_retry must be awaitable
            # but we patch it on the real asyncio module to skip actual waits.
            with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
                results = self.grader.grade_exam_batch(
                    exam_id=10,
                    student_id="21/52CS001",
                    responses=[{
                        "question_id": 5,
                        "question_text": "Define OOP.",
                        "model_answer": "Object-oriented programming.",
                        "rubric": "5 marks",
                        "max_marks": 5.0,
                        "answer_html": "OOP means objects.",
                    }],
                )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["question_id"], 5)
        self.assertEqual(results[0]["score"], 0.0)
        self.assertEqual(results[0]["confidence"], 0.0)


if __name__ == "__main__":
    unittest.main()
"""
tests/test_grader.py — Unit tests for services/grader.py

Mocking strategy:
  - Patch services.grader._get_groq_client to return a MagicMock Groq client.
    This avoids any real network calls and removes the GROQ_API_KEY requirement.
  - Patch services.grader.log_to_audit to assert it is called on failure paths
    without touching the database.
  - strip_html_tags runs for real — it is pure Python and makes tests more
    realistic (we catch any sanitizer regression here too).

Test map (matches Phase 1B spec):
  1.  test_parse_valid_json_response             — correct score and confidence
  2.  test_parse_score_capped_to_max_marks       — score > max_marks → clamped
  3.  test_parse_confidence_capped_to_1          — confidence > 1.0 → clamped
  4.  test_parse_malformed_json_returns_fallback — bad JSON → score=0 fallback
  5.  test_edge_case_blank_answer               — <5 chars → score=0, confidence=1.0
  6.  test_edge_case_answer_mirrors_question    — >80% similarity → score=0
  7.  test_api_exception_returns_fallback_and_logs_audit
  8.  test_grade_exam_batch_three_questions     — 3 inputs → 3 results

Extra tests for robustness:
  9.  test_parse_integer_confidence_coerced    — int 1 accepted as confidence
  10. test_edge_case_valid_answer_returns_none — passing answer → None (proceed)
  11. test_grade_single_response_blank_skips_api
  12. test_grade_exam_batch_echoes_question_id
  13. test_parse_missing_score_key_returns_fallback
  14. test_grade_exam_batch_partial_failure_isolates
"""

import json
import sys
import os
import unittest
from unittest.mock import MagicMock, patch, call

# Ensure the project root is on sys.path regardless of where pytest is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.grader import (
    _AI_FAILURE_FALLBACK,
    check_edge_cases,
    grade_exam_batch,
    grade_single_response,
    parse_ai_response,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_groq_response(payload: dict) -> MagicMock:
    """Return a MagicMock that looks like a Groq ChatCompletion response."""
    mock_response = MagicMock()
    mock_response.choices[0].message.content = json.dumps(payload)
    return mock_response


def _make_groq_client(payload: dict) -> MagicMock:
    """Return a mock Groq client whose create() returns a fixed payload."""
    client = MagicMock()
    client.chat.completions.create.return_value = _make_groq_response(payload)
    return client


# ---------------------------------------------------------------------------
# 1. parse_ai_response — valid JSON
# ---------------------------------------------------------------------------

class TestParseAiResponseValid(unittest.TestCase):
    def test_parse_valid_json_response(self):
        """Valid JSON with score and confidence within bounds passes through unchanged."""
        raw = json.dumps({"score": 7.5, "feedback": "Good answer.", "confidence": 0.9})
        result = parse_ai_response(raw, max_marks=10.0, exam_id=1, question_id=1)

        self.assertEqual(result["score"], 7.5)
        self.assertAlmostEqual(result["confidence"], 0.9)
        self.assertEqual(result["feedback"], "Good answer.")


# ---------------------------------------------------------------------------
# 2. Score hard-capped at max_marks
# ---------------------------------------------------------------------------

class TestParseAiResponseScoreCap(unittest.TestCase):
    def test_parse_score_capped_to_max_marks(self):
        """AI returning a score above max_marks must be silently clamped."""
        raw = json.dumps({"score": 15.0, "feedback": "Too high.", "confidence": 0.8})
        result = parse_ai_response(raw, max_marks=10.0, exam_id=1, question_id=2)

        self.assertEqual(result["score"], 10.0)  # clamped to max_marks

    def test_parse_negative_score_clamped_to_zero(self):
        """Negative scores are also clamped — model should never produce them but guard anyway."""
        raw = json.dumps({"score": -2.0, "feedback": "Negative.", "confidence": 0.5})
        result = parse_ai_response(raw, max_marks=10.0, exam_id=1, question_id=2)

        self.assertEqual(result["score"], 0.0)


# ---------------------------------------------------------------------------
# 3. Confidence hard-capped at 1.0
# ---------------------------------------------------------------------------

class TestParseAiResponseConfidenceCap(unittest.TestCase):
    def test_parse_confidence_capped_to_1(self):
        """Confidence above 1.0 must be clamped to exactly 1.0."""
        raw = json.dumps({"score": 5.0, "feedback": "OK.", "confidence": 1.8})
        result = parse_ai_response(raw, max_marks=10.0, exam_id=1, question_id=3)

        self.assertEqual(result["confidence"], 1.0)

    def test_parse_negative_confidence_clamped_to_zero(self):
        """Confidence below 0.0 is also illegal — clamp to 0.0."""
        raw = json.dumps({"score": 5.0, "feedback": "OK.", "confidence": -0.5})
        result = parse_ai_response(raw, max_marks=10.0, exam_id=1, question_id=3)

        self.assertEqual(result["confidence"], 0.0)


# ---------------------------------------------------------------------------
# 4. Malformed JSON → fallback
# ---------------------------------------------------------------------------

class TestParseAiResponseMalformedJson(unittest.TestCase):
    @patch("services.grader.log_to_audit")
    def test_parse_malformed_json_returns_fallback(self, mock_audit):
        """Non-JSON text returns the fallback dict with score=0 and logs to audit."""
        raw = "Sorry, I cannot grade this."
        result = parse_ai_response(raw, max_marks=10.0, exam_id=2, question_id=4)

        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["confidence"], 0.0)
        self.assertEqual(result["feedback"], _AI_FAILURE_FALLBACK["feedback"])
        mock_audit.assert_called_once()

    @patch("services.grader.log_to_audit")
    def test_parse_missing_score_key_returns_fallback(self, mock_audit):
        """JSON missing the 'score' key also triggers the fallback."""
        raw = json.dumps({"feedback": "Good.", "confidence": 0.9})
        result = parse_ai_response(raw, max_marks=10.0, exam_id=2, question_id=5)

        self.assertEqual(result["score"], 0.0)
        mock_audit.assert_called_once()

    @patch("services.grader.log_to_audit")
    def test_parse_markdown_fenced_json_returns_fallback(self, mock_audit):
        """
        If the model wraps its response in ```json ... ``` fences despite being
        told not to, json.loads will fail and the fallback must fire.
        This is a known Groq model misbehaviour worth guarding against.
        """
        raw = '```json\n{"score": 5.0, "feedback": "Fine.", "confidence": 0.8}\n```'
        result = parse_ai_response(raw, max_marks=10.0, exam_id=2, question_id=6)

        self.assertEqual(result["score"], 0.0)
        mock_audit.assert_called_once()


# ---------------------------------------------------------------------------
# 5. Edge case — blank answer
# ---------------------------------------------------------------------------

class TestCheckEdgeCasesBlank(unittest.TestCase):
    def test_edge_case_blank_answer(self):
        """Answers with fewer than 5 characters score 0 with confidence 1.0."""
        result = check_edge_cases("", "What is osmosis?")
        self.assertIsNotNone(result)
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["confidence"], 1.0)

    def test_edge_case_whitespace_only_answer(self):
        """Whitespace-only answers are treated as blank."""
        result = check_edge_cases("   \n  ", "What is osmosis?")
        self.assertIsNotNone(result)
        self.assertEqual(result["score"], 0.0)

    def test_edge_case_four_char_answer(self):
        """Exactly 4 characters (boundary) is still blank."""
        result = check_edge_cases("abcd", "What is osmosis?")
        self.assertIsNotNone(result)
        self.assertEqual(result["score"], 0.0)


# ---------------------------------------------------------------------------
# 6. Edge case — answer mirrors question
# ---------------------------------------------------------------------------

class TestCheckEdgeCasesPlagiarism(unittest.TestCase):
    def test_edge_case_answer_mirrors_question(self):
        """An answer that is >80% similar to the question text scores 0."""
        question = "Define the process by which water moves through a semi-permeable membrane."
        answer = "The process by which water moves through a semi-permeable membrane."  # ~95% similar
        result = check_edge_cases(answer, question)

        self.assertIsNotNone(result)
        self.assertEqual(result["score"], 0.0)
        self.assertIn("copy", result["feedback"].lower())

    def test_edge_case_valid_answer_returns_none(self):
        """A substantive, original answer must return None (proceed to grading)."""
        question = "Explain Newton's second law."
        answer = (
            "Newton's second law states that force equals mass multiplied by "
            "acceleration. This means a larger force produces a greater change "
            "in an object's motion."
        )
        result = check_edge_cases(answer, question)

        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# 7. Groq API exception → fallback + audit logged
# ---------------------------------------------------------------------------

class TestGradeSingleResponseApiException(unittest.TestCase):
    @patch("services.grader.log_to_audit")
    @patch("services.grader._get_groq_client")
    def test_api_exception_returns_fallback_and_logs_audit(
        self, mock_get_client, mock_audit
    ):
        """When Groq raises any exception, fallback is returned and audit is called."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = RuntimeError("Connection timeout")
        mock_get_client.return_value = mock_client

        result = grade_single_response(
            question_id=1,
            question_text="What is TCP/IP?",
            model_answer="A suite of communication protocols.",
            rubric="Award 5 marks for correct definition.",
            max_marks=5.0,
            student_answer_html="<p>TCP/IP is a protocol used for internet communication.</p>",
            exam_id=10,
        )

        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["confidence"], 0.0)
        mock_audit.assert_called_once()
        # Verify question_id appears in the details dict passed to log_to_audit.
        call_kwargs = mock_audit.call_args
        details = call_kwargs.kwargs.get("details", {})
        self.assertEqual(details.get("question_id"), 1)

    @patch("services.grader.log_to_audit")
    @patch("services.grader._get_groq_client")
    def test_missing_api_key_returns_fallback(self, mock_get_client, mock_audit):
        """EnvironmentError (missing key) follows the same fallback path."""
        mock_get_client.side_effect = EnvironmentError("GROQ_API_KEY is not set.")

        result = grade_single_response(
            question_id=2,
            question_text="Explain recursion.",
            model_answer="A function calling itself.",
            rubric="5 marks for correct definition.",
            max_marks=5.0,
            student_answer_html="<p>Recursion is when a function calls itself.</p>",
            exam_id=10,
        )

        self.assertEqual(result["score"], 0.0)
        mock_audit.assert_called_once()


# ---------------------------------------------------------------------------
# 8. grade_exam_batch — 3 questions → 3 results
# ---------------------------------------------------------------------------

class TestGradeExamBatch(unittest.TestCase):
    @patch("services.grader._get_groq_client")
    def test_grade_exam_batch_three_questions(self, mock_get_client):
        """Batch grading of 3 questions returns exactly 3 result dicts."""
        # Each call to create() returns a different score so we can assert order.
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            _make_groq_response({"score": 4.0, "feedback": "Good.", "confidence": 0.9}),
            _make_groq_response({"score": 2.5, "feedback": "Partial.", "confidence": 0.7}),
            _make_groq_response({"score": 0.0, "feedback": "Wrong.", "confidence": 0.95}),
        ]
        mock_get_client.return_value = mock_client

        responses = [
            {
                "question_id": 101,
                "question_text": "What is RAM?",
                "model_answer": "Random Access Memory.",
                "rubric": "4 marks for full definition.",
                "max_marks": 4.0,
                "answer_html": "<p>RAM stands for Random Access Memory used for temporary storage.</p>",
            },
            {
                "question_id": 102,
                "question_text": "Define an algorithm.",
                "model_answer": "A step-by-step procedure to solve a problem.",
                "rubric": "5 marks for correct definition with example.",
                "max_marks": 5.0,
                "answer_html": "<p>An algorithm is a set of steps.</p>",
            },
            {
                "question_id": 103,
                "question_text": "What is a compiler?",
                "model_answer": "A program that translates source code to machine code.",
                "rubric": "3 marks.",
                "max_marks": 3.0,
                "answer_html": "<p>I don't know.</p>",
            },
        ]

        results = grade_exam_batch(exam_id=5, student_id="21/52CS001", responses=responses)

        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["score"], 4.0)
        self.assertEqual(results[1]["score"], 2.5)
        self.assertEqual(results[2]["score"], 0.0)

    @patch("services.grader._get_groq_client")
    def test_grade_exam_batch_echoes_question_id(self, mock_get_client):
        """Every result dict must contain the question_id from the input."""
        mock_client = MagicMock()
        mock_client.chat.completions.create.return_value = _make_groq_response(
            {"score": 3.0, "feedback": "OK.", "confidence": 0.85}
        )
        mock_get_client.return_value = mock_client

        responses = [
            {
                "question_id": 999,
                "question_text": "What is SQL?",
                "model_answer": "Structured Query Language.",
                "rubric": "5 marks.",
                "max_marks": 5.0,
                "answer_html": "<p>SQL is used to query databases.</p>",
            }
        ]

        results = grade_exam_batch(exam_id=7, student_id="21/52CS002", responses=responses)

        self.assertEqual(results[0]["question_id"], 999)

    @patch("services.grader.log_to_audit")
    @patch("services.grader._get_groq_client")
    def test_grade_exam_batch_partial_failure_isolates(self, mock_get_client, mock_audit):
        """
        If one question's grading fails, the other questions still get results.
        A single bad response must not abort the whole batch.
        """
        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = [
            _make_groq_response({"score": 5.0, "feedback": "Excellent.", "confidence": 0.95}),
            RuntimeError("Groq rate limit"),  # second call fails
            _make_groq_response({"score": 2.0, "feedback": "Partial.", "confidence": 0.75}),
        ]
        mock_get_client.return_value = mock_client

        responses = [
            {
                "question_id": 201,
                "question_text": "Define OOP.",
                "model_answer": "Object-Oriented Programming.",
                "rubric": "5 marks.",
                "max_marks": 5.0,
                "answer_html": "<p>OOP is a programming paradigm based on objects.</p>",
            },
            {
                "question_id": 202,
                "question_text": "What is inheritance?",
                "model_answer": "A class deriving properties from another class.",
                "rubric": "5 marks.",
                "max_marks": 5.0,
                "answer_html": "<p>Inheritance allows one class to use properties of another.</p>",
            },
            {
                "question_id": 203,
                "question_text": "What is polymorphism?",
                "model_answer": "One interface, many implementations.",
                "rubric": "5 marks.",
                "max_marks": 5.0,
                "answer_html": "<p>Polymorphism means many forms in programming.</p>",
            },
        ]

        results = grade_exam_batch(exam_id=8, student_id="21/52CS003", responses=responses)

        # All three questions must get a result — the failed one gets the fallback.
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["score"], 5.0)
        self.assertEqual(results[1]["score"], 0.0)   # fallback from RuntimeError
        self.assertEqual(results[1]["confidence"], 0.0)
        self.assertEqual(results[2]["score"], 2.0)


# ---------------------------------------------------------------------------
# 9. Bonus — integer confidence is coerced to float
# ---------------------------------------------------------------------------

class TestParseIntegerConfidence(unittest.TestCase):
    def test_parse_integer_confidence_coerced(self):
        """Groq sometimes returns confidence as int 1 — must be accepted as 1.0."""
        raw = json.dumps({"score": 8.0, "feedback": "Very good.", "confidence": 1})
        result = parse_ai_response(raw, max_marks=10.0, exam_id=1, question_id=7)

        self.assertIsInstance(result["confidence"], float)
        self.assertEqual(result["confidence"], 1.0)


# ---------------------------------------------------------------------------
# 10. grade_single_response — blank answer never reaches the API
# ---------------------------------------------------------------------------

class TestGradeSingleResponseBlankSkipsApi(unittest.TestCase):
    @patch("services.grader._get_groq_client")
    def test_grade_single_response_blank_skips_api(self, mock_get_client):
        """Blank HTML answer is caught by check_edge_cases before any API call."""
        result = grade_single_response(
            question_id=1,
            question_text="What is RAM?",
            model_answer="Random Access Memory.",
            rubric="4 marks.",
            max_marks=4.0,
            student_answer_html="<p></p>",  # Quill empty paragraph
            exam_id=3,
        )

        mock_get_client.assert_not_called()
        self.assertEqual(result["score"], 0.0)
        self.assertEqual(result["confidence"], 1.0)


# ---------------------------------------------------------------------------
# 11. Fallback dict is never mutated across calls
# ---------------------------------------------------------------------------

class TestFallbackImmutability(unittest.TestCase):
    @patch("services.grader.log_to_audit")
    def test_fallback_not_mutated_between_calls(self, _mock_audit):
        """
        Each failure must return a fresh copy of the fallback dict.
        If callers mutate the returned dict, it must not affect future calls.
        """
        raw = "not json at all"

        result1 = parse_ai_response(raw, max_marks=5.0, exam_id=1, question_id=1)
        result1["score"] = 99.0  # mutate the returned dict

        result2 = parse_ai_response(raw, max_marks=5.0, exam_id=1, question_id=2)

        self.assertEqual(result2["score"], 0.0)  # must still be the fallback value


if __name__ == "__main__":
    unittest.main()
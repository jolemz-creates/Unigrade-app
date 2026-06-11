"""
pages/lecturer/grading_review.py
─────────────────────────────────
Manual grading review dashboard for Lecturers and Chief Examiners.

Loads all student responses for the selected exam, computes per-question
statistics, auto-flags suspicious AI scores, and provides a manual override
form with a mandatory reason field. Every override is written to audit_log.
"""

import json
import statistics
import streamlit as st

from models.student_repo import get_responses_for_exam, apply_manual_override
from models.exam_repo import get_exam_by_id, get_questions_by_exam
from services.audit import log_to_audit


# ── Page Guard ────────────────────────────────────────────────────────────────
# Required on every protected page per CLAUDE.md §3.3.

def render_grading_review() -> None:
    """Main entry point called by app.py after staff routing."""

    if not st.session_state.get("logged_in"):
        st.error("Unauthorized. Please log in.")
        st.stop()

    if st.session_state.get("role") not in ["Lecturer", "Chief Examiner"]:
        st.error("Access denied.")
        st.stop()

    # ── Session state defaults ─────────────────────────────────────────────────
    st.session_state.setdefault("filter_flagged_only", False)
    st.session_state.setdefault("override_mode", False)
    # Tracks the specific response_id whose override form is currently open.
    # None means no form is open. Kept separately from override_mode because
    # override_mode is a global flag (§5.3) while this is per-card state.
    st.session_state.setdefault("_active_override_id", None)

    # ── Exam Selection Guard ───────────────────────────────────────────────────
    exam_id: int | None = st.session_state.get("selected_exam_id")
    if not exam_id:
        st.info(
            "No exam selected. Go to your dashboard and click **Review Grades** "
            "on the exam you want to inspect."
        )
        return

    exam = get_exam_by_id(exam_id)
    if not exam:
        st.error(f"Exam ID {exam_id} not found.")
        return

    # ── Page Header ────────────────────────────────────────────────────────────
    st.markdown(
        f"<h2 style='color:#004D40;margin-bottom:4px;'>"
        f"Grading Review</h2>"
        f"<p style='color:#555;margin-top:0;'>"
        f"{exam['course_code']} — {exam['title']} &nbsp;·&nbsp; "
        f"Status: <strong>{exam['status']}</strong></p>",
        unsafe_allow_html=True,
    )

    # ── Load Data ──────────────────────────────────────────────────────────────
    responses: list[dict] = get_responses_for_exam(exam_id)
    questions: list[dict] = get_questions_by_exam(exam_id)
    question_map: dict[int, dict] = {q["id"]: q for q in questions}

    # Denormalise max_points and question_number onto each response so all
    # downstream helpers have a self-contained dict to work with.
    for r in responses:
        q = question_map.get(r["question_id"], {})
        r["max_points"] = q.get("max_points", 0)
        r["question_number"] = q.get("question_number", str(r["question_id"]))

    if not responses:
        st.info("No student responses have been submitted for this exam yet.")
        return

    # ── Statistics & Flags ─────────────────────────────────────────────────────
    class_stats = _compute_class_stats(responses)

    flagged_ids: set[int] = set()
    for r in responses:
        r["_flags"] = _compute_flags(r, class_stats)
        if r["_flags"]:
            flagged_ids.add(r["id"])

    total_responses = len(responses)
    flagged_count = len(flagged_ids)
    overridden_count = sum(
        1 for r in responses if r.get("manual_override") is not None
    )

    # ── Summary Metrics ────────────────────────────────────────────────────────
    col_total, col_flagged, col_overridden = st.columns(3)
    col_total.metric("Total Responses", total_responses)
    col_flagged.metric("Flagged for Review", flagged_count)
    col_overridden.metric("Manually Overridden", overridden_count)

    st.divider()

    # ── Filter Toggle ──────────────────────────────────────────────────────────
    st.toggle("Show flagged responses only", key="filter_flagged_only")

    display_responses = (
        [r for r in responses if r["id"] in flagged_ids]
        if st.session_state.filter_flagged_only
        else responses
    )

    if not display_responses:
        st.success("✓ No flagged responses to display.")
        return

    # ── Group by Question Number ───────────────────────────────────────────────
    # Sort question numbers naturally: "1" < "1a" < "1b" < "2" etc.
    by_question: dict[str, list[dict]] = {}
    for r in display_responses:
        by_question.setdefault(r["question_number"], []).append(r)

    sorted_qnums = sorted(by_question.keys(), key=_question_sort_key)

    for qnum in sorted_qnums:
        q_responses = by_question[qnum]
        max_pts = q_responses[0].get("max_points", "?")
        q_flagged = sum(1 for r in q_responses if r["_flags"])

        header = f"Question {qnum}  ·  {len(q_responses)} response(s)  ·  Max: {max_pts} pts"
        if q_flagged:
            header += f"  ·  ⚑ {q_flagged} flagged"

        with st.expander(header, expanded=bool(q_flagged)):
            for r in q_responses:
                _render_response_card(r, exam_id)
                st.divider()


# ── Sorting Helper ─────────────────────────────────────────────────────────────

def _question_sort_key(qnum: str) -> tuple:
    """
    Produces a sort tuple so that question numbers sort naturally:
    "1" → (1, "")  "1a" → (1, "a")  "1b" → (1, "b")  "2" → (2, "")
    Falls back to string sort for anything that doesn't match the pattern.
    """
    numeric = ""
    alpha = ""
    for i, ch in enumerate(qnum):
        if ch.isdigit():
            numeric += ch
        else:
            alpha = qnum[i:]
            break
    try:
        return (int(numeric), alpha)
    except ValueError:
        return (0, qnum)


# ── Statistics ─────────────────────────────────────────────────────────────────

def _compute_class_stats(responses: list[dict]) -> dict[int, dict]:
    """
    Returns {question_id: {"mean": float, "stdev": float}}.
    stdev is 0.0 when fewer than 2 scores exist (anomaly check is then skipped).
    Only includes responses where ai_score is not None.
    """
    by_question: dict[int, list[float]] = {}
    for r in responses:
        score = r.get("ai_score")
        if score is not None:
            by_question.setdefault(r["question_id"], []).append(float(score))

    result: dict[int, dict] = {}
    for qid, scores in by_question.items():
        mean = statistics.mean(scores)
        stdev = statistics.stdev(scores) if len(scores) >= 2 else 0.0
        result[qid] = {"mean": mean, "stdev": stdev}
    return result


# ── Flagging Logic ─────────────────────────────────────────────────────────────

def _is_hedging(score: float | None, max_points: int) -> bool:
    """
    True when the score's fractional part is exactly 0.5, indicating the AI
    hedged between two integer marks (e.g. 2.5/5, 4.5/10).
    Scores of 0 and max_points are excluded — those are unambiguous decisions.
    """
    if score is None:
        return False
    if score == 0 or score == max_points:
        return False
    return round(score % 1, 10) == 0.5


def _is_anomaly(score: float | None, question_id: int, stats: dict) -> bool:
    """True when score is more than 2 standard deviations from the class mean."""
    if score is None or question_id not in stats:
        return False
    q = stats[question_id]
    if q["stdev"] == 0.0:
        # Only one student submitted this question — can't compute an outlier.
        return False
    return abs(score - q["mean"]) > 2 * q["stdev"]


def _compute_flags(response: dict, stats: dict) -> list[str]:
    """Returns a list of flag label strings for a given response."""
    flags: list[str] = []
    score = response.get("ai_score")
    confidence = response.get("ai_confidence")
    qid = response["question_id"]
    max_pts = response.get("max_points", 0)

    if confidence is not None and confidence < 0.6:
        flags.append("Low Confidence")
    if _is_hedging(score, max_pts):
        flags.append("Hedging")
    if _is_anomaly(score, qid, stats):
        flags.append("Anomaly")
    return flags


# ── Badge Renderer ─────────────────────────────────────────────────────────────

_BADGE_STYLES: dict[str, tuple[str, str]] = {
    "Low Confidence": ("#E65100", "#FFF3E0"),   # Deep orange
    "Hedging":        ("#1565C0", "#E3F2FD"),   # Blue
    "Anomaly":        ("#C62828", "#FFEBEE"),   # Red
    "Manually Reviewed": ("#2E7D32", "#E8F5E9"), # Green
}


def _render_flag_badges(flags: list[str]) -> None:
    if not flags:
        return
    parts = []
    for flag in flags:
        color, bg = _BADGE_STYLES.get(flag, ("#333333", "#EEEEEE"))
        parts.append(
            f'<span style="background:{bg};color:{color};border:1px solid {color};'
            f'border-radius:4px;padding:2px 10px;font-size:12px;font-weight:600;'
            f'margin-right:6px;white-space:nowrap;">{flag}</span>'
        )
    st.markdown("".join(parts), unsafe_allow_html=True)


# ── Audit Logger for Overrides ─────────────────────────────────────────────────

def _log_override(
    question_id: int,
    question_number: str,
    ai_score: float | None,
    new_score: float,
    reason: str,
    exam_id: int,
) -> None:
    """Writes a structured override entry to audit_log per CLAUDE.md §8.3."""
    log_to_audit(
        action=f"Manual override on Q{question_number}",
        user_id=st.session_state.user_id,
        exam_id=exam_id,
        details=json.dumps({
            "question_id": question_id,
            "original_ai_score": ai_score,
            "new_score": new_score,
            "reason": reason,
        }),
    )


# ── Response Card ──────────────────────────────────────────────────────────────

def _render_response_card(r: dict, exam_id: int) -> None:
    """
    Renders a single student response with:
      - Student ID, effective score, AI confidence
      - Flag badges
      - Truncated answer preview and AI feedback
      - "Override AI Score" button or the open override form
    """
    response_id: int = r["id"]
    ai_score: float | None = r.get("ai_score")
    ai_confidence: float | None = r.get("ai_confidence")
    ai_feedback: str = r.get("ai_feedback") or ""
    manual_override: float | None = r.get("manual_override")
    max_pts: int = r.get("max_points", 0)
    flags: list[str] = r.get("_flags", [])
    sanitized_text: str = r.get("sanitized_text") or ""
    student_id: str = r.get("student_id", "Unknown")
    question_id: int = r["question_id"]
    question_number: str = r.get("question_number", str(question_id))

    # The score shown to the lecturer is the override if one exists, else AI score.
    effective_score = manual_override if manual_override is not None else ai_score
    score_label = (
        f"{effective_score}/{max_pts}" if effective_score is not None else f"—/{max_pts}"
    )

    # ── Header Row ─────────────────────────────────────────────────────────────
    col_student, col_score, col_conf = st.columns([3, 2, 2])

    with col_student:
        st.markdown(f"**Student:** `{student_id}`")

    with col_score:
        override_marker = " ✏️" if manual_override is not None else ""
        st.markdown(f"**Score:** {score_label}{override_marker}")

    with col_conf:
        if ai_confidence is not None:
            st.markdown(f"**AI Confidence:** {ai_confidence * 100:.0f}%")
        else:
            st.markdown("**AI Confidence:** —")

    # ── Flag Badges ────────────────────────────────────────────────────────────
    badge_flags = list(flags)
    if manual_override is not None:
        badge_flags.append("Manually Reviewed")
    _render_flag_badges(badge_flags)

    st.markdown("&nbsp;", unsafe_allow_html=True)  # visual breathing room

    # ── Answer Preview ─────────────────────────────────────────────────────────
    if sanitized_text:
        preview = sanitized_text[:300] + ("…" if len(sanitized_text) > 300 else "")
        st.markdown(f"**Student's Answer:** {preview}")
    else:
        st.markdown("**Student's Answer:** *(blank)*")

    # ── AI Feedback ────────────────────────────────────────────────────────────
    if ai_feedback:
        st.markdown(
            f"<div style='background:#F0F2F6;border-radius:6px;padding:10px 14px;"
            f"margin-top:6px;font-size:14px;color:#333;'>"
            f"<strong>AI Feedback:</strong> {ai_feedback}</div>",
            unsafe_allow_html=True,
        )

    st.markdown("&nbsp;", unsafe_allow_html=True)

    # ── Override Form or Button ────────────────────────────────────────────────
    is_overriding = st.session_state.get("_active_override_id") == response_id

    if not is_overriding:
        if st.button(
            "Override AI Score",
            key=f"open_override_{response_id}",
            type="secondary",
        ):
            st.session_state["_active_override_id"] = response_id
            st.session_state.override_mode = True
            st.rerun()

    else:
        # ── Override Form ──────────────────────────────────────────────────────
        st.markdown(
            "<hr style='border:1px solid #E0E0E0;margin:12px 0;'>",
            unsafe_allow_html=True,
        )
        st.markdown("**Override AI Score**")

        default_score = float(effective_score) if effective_score is not None else 0.0
        new_score = st.number_input(
            f"New score (0 – {max_pts})",
            min_value=0.0,
            max_value=float(max_pts),
            value=default_score,
            step=0.5,
            key=f"new_score_{response_id}",
        )

        override_reason = st.text_area(
            "Reason for override (required)",
            key=f"reason_{response_id}",
            placeholder=(
                "Explain why you are overriding the AI score. "
                "This will be permanently recorded in the audit log."
            ),
            height=100,
        )

        col_save, col_cancel, _ = st.columns([1, 1, 4])

        with col_save:
            if st.button(
                "Save Override",
                key=f"save_{response_id}",
                type="primary",
            ):
                if not override_reason.strip():
                    st.warning("A reason is required before saving an override.")
                else:
                    try:
                        apply_manual_override(
                            response_id=response_id,
                            score=new_score,
                            overridden_by=st.session_state.user_id,
                            reason=override_reason.strip(),
                        )
                        _log_override(
                            question_id=question_id,
                            question_number=question_number,
                            ai_score=ai_score,
                            new_score=new_score,
                            reason=override_reason.strip(),
                            exam_id=exam_id,
                        )
                        st.session_state["_active_override_id"] = None
                        st.session_state.override_mode = False
                        st.success(
                            f"Score for {student_id} / Q{question_number} "
                            f"overridden to {new_score}/{max_pts}."
                        )
                        st.rerun()
                    except ValueError as exc:
                        # apply_manual_override raises ValueError if the exam
                        # session is not yet in 'Submitted' state — guard per §8.2.
                        st.error(str(exc))

        with col_cancel:
            if st.button("Cancel", key=f"cancel_{response_id}"):
                st.session_state["_active_override_id"] = None
                st.session_state.override_mode = False
                st.rerun()
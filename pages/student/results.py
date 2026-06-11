"""
pages/student/results.py
─────────────────────────
Post-publication results view for students.

A student sees results only for exams where:
  • The exam is chief_approved = TRUE and status = 'Closed'
  • They have a Submitted (or Auto-Submitted) exam session

Per exam the page shows:
  • Total score (sum of effective scores) and max possible score
  • Per-question breakdown: question number, effective score, AI feedback,
    and a "Graded by AI" or "Manually Reviewed" label

Students cannot see any other student's data. All queries are scoped to
st.session_state.user_id (matric number).

Prerequisite: add get_approved_exams_for_student() from exam_repo_patch_2.py
              to models/exam_repo.py before deploying this page.
"""

import streamlit as st

from models.exam_repo import get_approved_exams_for_student, get_questions_by_exam
from models.student_repo import get_responses_for_student
from services.result_slip import generate_result_slip


# ── Main Entry Point ──────────────────────────────────────────────────────────

def render_results() -> None:
    """Called by app.py after student routing."""

    # ── Page Guard ─────────────────────────────────────────────────────────────
    if not st.session_state.get("logged_in"):
        st.error("Unauthorized. Please log in.")
        st.stop()

    if st.session_state.get("role") != "Student":
        st.error("Access denied.")
        st.stop()

    student_id: str = st.session_state.user_id
    student_name: str = st.session_state.get("user_name") or student_id

    # ── Page Header ────────────────────────────────────────────────────────────
    st.markdown(
        "<h2 style='color:#004D40;margin-bottom:4px;'>My Results</h2>"
        f"<p style='color:#555;margin-top:0;'>{student_name} &nbsp;·&nbsp; "
        f"<code>{student_id}</code></p>",
        unsafe_allow_html=True,
    )

    # ── Load Approved Exams ────────────────────────────────────────────────────
    exams: list[dict] = get_approved_exams_for_student(student_id)

    if not exams:
        st.info(
            "No results are available yet. Results appear here once your "
            "lecturer and Chief Examiner have reviewed and approved your grades."
        )
        return

    # ── Per-Exam Sections ──────────────────────────────────────────────────────
    for exam in exams:
        exam_id: int = exam["id"]

        responses: list[dict] = get_responses_for_student(
            exam_id=exam_id, student_id=student_id
        )
        questions: list[dict] = get_questions_by_exam(exam_id)
        question_map: dict[int, dict] = {q["id"]: q for q in questions}

        _render_exam_result(exam, responses, question_map)

        # ── Download Result Slip ───────────────────────────────────────────────
        student_data = {
            "matric_no":  student_id,
            "name":       student_name,
            "department": st.session_state.get("department", ""),
            "level":      st.session_state.get("level", ""),
        }
        pdf_bytes = generate_result_slip(student_data, exam, responses, question_map)
        filename = (
            f"result_slip_{student_id.replace('/', '-')}"
            f"_{exam['course_code']}_{exam.get('session_code', exam['id'])}.pdf"
        )
        st.download_button(
            label="Download Result Slip (PDF)",
            data=pdf_bytes,
            file_name=filename,
            mime="application/pdf",
            key=f"dl_{exam['id']}",
        )

        st.divider()


# ── Exam Result Section ────────────────────────────────────────────────────────

def _render_exam_result(
    exam: dict,
    responses: list[dict],
    question_map: dict[int, dict],
) -> None:
    """Renders one exam's full result: summary score card + per-question rows."""

    course_code: str = exam["course_code"]
    title: str = exam["title"]
    approved_at: str = (exam.get("approved_at") or "")[:10]   # date only

    st.markdown(
        f"<h3 style='color:#004D40;margin-bottom:2px;'>{course_code} — {title}</h3>"
        + (
            f"<p style='color:#777;font-size:13px;margin-top:0;'>"
            f"Results released: {approved_at}</p>"
            if approved_at else ""
        ),
        unsafe_allow_html=True,
    )

    if not responses:
        st.warning("Your responses were not found for this exam. Contact your lecturer.")
        return

    # ── Score Totals ───────────────────────────────────────────────────────────
    total_earned: float = 0.0
    total_possible: float = 0.0

    for r in responses:
        score = _effective_score(r)
        q = question_map.get(r["question_id"], {})
        max_pts = float(q.get("max_points", 0))
        if score is not None:
            total_earned += score
        total_possible += max_pts

    percentage = (total_earned / total_possible * 100) if total_possible > 0 else 0.0

    # Score summary card
    score_color = (
        "#2E7D32" if percentage >= 50 else "#C62828"
    )
    st.markdown(
        f"<div style='background:#F0F2F6;border-radius:8px;padding:16px 20px;"
        f"margin:8px 0 16px 0;display:inline-block;'>"
        f"<span style='font-size:28px;font-weight:700;color:{score_color};'>"
        f"{total_earned:.1f} / {total_possible:.0f}</span>"
        f"<span style='font-size:16px;color:#555;margin-left:12px;'>"
        f"({percentage:.1f}%)</span>"
        f"</div>",
        unsafe_allow_html=True,
    )

    # ── Per-Question Breakdown ─────────────────────────────────────────────────
    # Sort responses by question_number for a consistent reading order.
    sorted_responses = sorted(
        responses,
        key=lambda r: _question_sort_key(
            question_map.get(r["question_id"], {}).get("question_number", "")
        ),
    )

    for r in sorted_responses:
        _render_question_row(r, question_map)


# ── Question Row ───────────────────────────────────────────────────────────────

def _render_question_row(r: dict, question_map: dict[int, dict]) -> None:
    """Renders a single question's score, label, and AI feedback."""

    q: dict = question_map.get(r["question_id"], {})
    question_number: str = q.get("question_number") or str(r["question_id"])
    max_pts: float = float(q.get("max_points", 0))

    score = _effective_score(r)
    score_label = f"{score:.1f}" if score is not None else "—"
    is_manual = r.get("manual_override") is not None
    ai_feedback: str = r.get("ai_feedback") or ""

    with st.container():
        col_qnum, col_score, col_badge = st.columns([2, 2, 3])

        with col_qnum:
            st.markdown(f"**Question {question_number}**")

        with col_score:
            st.markdown(f"**{score_label} / {max_pts:.0f}**")

        with col_badge:
            if is_manual:
                st.markdown(
                    '<span style="background:#E8F5E9;color:#2E7D32;border:1px solid #2E7D32;'
                    'border-radius:4px;padding:2px 8px;font-size:12px;font-weight:600;">'
                    "Manually Reviewed</span>",
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<span style="background:#E3F2FD;color:#1565C0;border:1px solid #1565C0;'
                    'border-radius:4px;padding:2px 8px;font-size:12px;font-weight:600;">'
                    "Graded by AI</span>",
                    unsafe_allow_html=True,
                )

        if ai_feedback:
            st.markdown(
                f"<div style='background:#F5F7F8;border-left:3px solid #004D40;"
                f"padding:8px 12px;margin:4px 0 8px 0;font-size:13px;color:#333;"
                f"border-radius:0 4px 4px 0;'>{ai_feedback}</div>",
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                "<div style='color:#aaa;font-size:12px;margin-bottom:8px;'>"
                "No feedback recorded.</div>",
                unsafe_allow_html=True,
            )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _effective_score(r: dict) -> float | None:
    """Returns manual_override if the lecturer set one, otherwise ai_score."""
    if r.get("manual_override") is not None:
        return float(r["manual_override"])
    if r.get("ai_score") is not None:
        return float(r["ai_score"])
    return None


def _question_sort_key(qnum: str) -> tuple:
    """Natural sort for question numbers: '1' < '1a' < '1b' < '2'."""
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
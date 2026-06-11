# pages/lecturer/dashboard.py
# UniGrade — Lecturer Dashboard
# Exam list with status badges, question counts, and action buttons.
# Create New Exam form with Quill instructions editor.
#
# Navigation note: clicking "Edit Questions" sets st.session_state.staff_page =
# "question_editor". app.py must dispatch on this key for the nav to work.
# See app.py's staff portal render path.
#
# Publish gate note: "Publish Exam to Students" is only shown when
# exam['chief_approved'] == True (per Phase 2B spec). In the default workflow,
# a Chief Examiner must approve the exam before it can be published. That
# approval UI lives in pages/chief_examiner/approval.py (Phase 3).
# Until then, lecturers can create and edit Draft exams freely.

from datetime import datetime

import streamlit as st
from streamlit_quill import st_quill

from models.exam_repo import (
    create_exam,
    get_exams_by_lecturer,
    get_questions_by_exam,
    update_exam_status,
)
from services.audit import log_to_audit


# ── Status badge colours ──────────────────────────────────────────────────────
_STATUS_COLOUR = {
    "Draft":     "#757575",   # Grey
    "Published": "#004D40",   # Unilorin Green
    "Closed":    "#E65100",   # Deep Orange
}

# ── Lecturer Quill toolbar (NO tables — per CLAUDE.md §7.2 / ARCHITECT.md §2) ─
_LECTURER_TOOLBAR = ["bold", "italic", "underline", "bullet", "number"]


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def render_dashboard() -> None:
    """
    Main entry point for the lecturer exam management dashboard.
    Called from app.py when role in ['Lecturer', 'Chief Examiner'] and
    st.session_state.staff_page == 'dashboard'.
    """
    # ── Page guard ────────────────────────────────────────────────────────────
    if not st.session_state.get("logged_in"):
        st.error("Unauthorized. Please log in.")
        st.stop()

    if st.session_state.get("role") not in ["Lecturer", "Chief Examiner"]:
        st.error("Access denied. This page is for Lecturers only.")
        st.stop()

    # ── Session state defaults ────────────────────────────────────────────────
    st.session_state.setdefault("selected_exam_id", None)
    st.session_state.setdefault("staff_page", "dashboard")
    st.session_state.setdefault("show_create_form", False)
    # Grading dashboard keys (initialised here so other pages don't error)
    st.session_state.setdefault("filter_flagged_only", False)
    st.session_state.setdefault("override_mode", False)

    lecturer_id: int = st.session_state.user_id

    # ── Page header ───────────────────────────────────────────────────────────
    st.markdown(
        f"""
        <div style="
            max-width: 800px;
            margin-bottom: 28px;
        ">
            <h2 style="color:#004D40; margin-bottom: 2px;">Lecturer Dashboard</h2>
            <p style="color:#777; margin: 0;">
                Welcome, <strong>{st.session_state.user_name}</strong>
                &nbsp;·&nbsp; {st.session_state.get('department', '')}
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Exam list header + create button ──────────────────────────────────────
    header_col, btn_col = st.columns([5, 2])

    with header_col:
        st.markdown(
            "<h3 style='color:#333; margin-bottom: 0;'>Your Exams</h3>",
            unsafe_allow_html=True,
        )

    with btn_col:
        toggle_label = (
            "✕ Cancel"
            if st.session_state.show_create_form
            else "＋ Create New Exam"
        )
        if st.button(toggle_label, use_container_width=True, key="toggle_create_form"):
            st.session_state.show_create_form = not st.session_state.show_create_form
            st.rerun()

    # ── Create exam form (collapsible) ────────────────────────────────────────
    if st.session_state.show_create_form:
        _render_create_exam_form(lecturer_id)
        st.divider()

    # ── Load exams ────────────────────────────────────────────────────────────
    try:
        exams = get_exams_by_lecturer(lecturer_id)
    except Exception as exc:
        log_to_audit(
            action="Dashboard exam load failure",
            user_id=str(lecturer_id),
            details={"error": str(exc)},
        )
        st.error("Could not load your exams. Please refresh the page.")
        return

    if not exams:
        st.markdown("<br>", unsafe_allow_html=True)
        st.info(
            "You have no exams yet. Click **＋ Create New Exam** above to get started."
        )
        return

    # ── Exam table ────────────────────────────────────────────────────────────
    _render_exam_table(exams, lecturer_id)


# ──────────────────────────────────────────────────────────────────────────────
# CREATE EXAM FORM
# ──────────────────────────────────────────────────────────────────────────────

def _render_create_exam_form(lecturer_id: int) -> None:
    """
    Collapsible form for creating a new exam.
    Intentionally NOT using st.form() — st_quill does not flush correctly
    inside a Streamlit form context. Inputs persist via their widget keys.
    """
    st.markdown(
        """
        <div style="
            background: white;
            border-radius: 8px;
            padding: 20px 24px;
            margin: 12px 0 16px 0;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            border-left: 4px solid #004D40;
        ">
            <h4 style="color:#004D40; margin: 0 0 16px 0;">New Exam Details</h4>
        </div>
        """,
        unsafe_allow_html=True,
    )

    col_code, col_title = st.columns(2)

    with col_code:
        course_code = st.text_input(
            "Course Code *",
            key="new_exam_course_code",
            placeholder="e.g. CSC201",
        )

    with col_title:
        title = st.text_input(
            "Exam Title *",
            key="new_exam_title",
            placeholder="e.g. Mid-Semester Examination",
        )

    duration = st.number_input(
        "Duration (minutes) *",
        min_value=10,
        max_value=300,
        value=60,
        step=5,
        key="new_exam_duration",
    )

    st.markdown(
        "<p style='font-size:0.85rem; color:#555; margin: 8px 0 4px 0;'>"
        "Exam Instructions <span style='color:#999;'>(optional)</span>:</p>",
        unsafe_allow_html=True,
    )

    # st_quill returns None when no change has occurred yet. The stored
    # session state value (or "") is the source of truth for the instructions.
    instructions_output = st_quill(
        placeholder="Enter instructions visible to students before they begin...",
        key="new_exam_instructions",
        toolbar=_LECTURER_TOOLBAR,
    )

    # Resolve instructions: prefer the widget's latest output; fall back to
    # whatever was previously stored in session state (handles page reruns).
    instructions: str = (
        instructions_output
        if instructions_output is not None
        else st.session_state.get("new_exam_instructions", "")
    )

    st.markdown("<br>", unsafe_allow_html=True)

    _, save_col = st.columns([4, 1])
    with save_col:
        if st.button("💾 Save Draft", type="primary", use_container_width=True, key="save_draft_btn"):
            _handle_create_exam(
                lecturer_id=lecturer_id,
                course_code=course_code,
                title=title,
                instructions=instructions,
                duration=int(duration),
            )


def _handle_create_exam(
    lecturer_id: int,
    course_code: str,
    title: str,
    instructions: str,
    duration: int,
) -> None:
    """
    Validate inputs, create the exam row, log to audit, and reset the form.
    On failure the form stays open and inputs are preserved for correction.
    session_code is generated inside exam_repo.create_exam() as:
        f"{year}-{course_code}-{title[:6].upper()}"
    """
    # ── Validation ────────────────────────────────────────────────────────────
    errors = []
    if not course_code.strip():
        errors.append("Course Code is required.")
    if not title.strip():
        errors.append("Exam Title is required.")
    if duration < 10:
        errors.append("Duration must be at least 10 minutes.")

    if errors:
        for msg in errors:
            st.error(msg)
        return

    clean_code = course_code.strip().upper()
    clean_title = title.strip()

    # ── Persist ───────────────────────────────────────────────────────────────
    try:
        exam_id = create_exam(
            lecturer_id=lecturer_id,
            course_code=clean_code,
            title=clean_title,
            instructions=instructions or "",
            duration=duration,
        )
    except Exception as exc:
        log_to_audit(
            action="Exam creation failure",
            user_id=str(lecturer_id),
            details={
                "error": str(exc),
                "course_code": clean_code,
                "title": clean_title,
            },
        )
        st.error(
            f"Could not create exam — {exc}. "
            "Your inputs have been preserved. Please try again."
        )
        return

    # ── Audit ─────────────────────────────────────────────────────────────────
    log_to_audit(
        action=f"Exam created: {clean_code} — {clean_title}",
        user_id=str(lecturer_id),
        exam_id=exam_id,
        details={
            "course_code": clean_code,
            "title": clean_title,
            "duration": duration,
        },
    )

    # ── Reset form ────────────────────────────────────────────────────────────
    # Remove widget keys so they revert to defaults on the next open.
    for key in (
        "new_exam_course_code",
        "new_exam_title",
        "new_exam_duration",
        "new_exam_instructions",
    ):
        st.session_state.pop(key, None)

    st.session_state.show_create_form = False
    st.success(f"✅ '{clean_title}' created as a Draft. Add questions via Edit Questions.")
    st.rerun()


# ──────────────────────────────────────────────────────────────────────────────
# EXAM TABLE
# ──────────────────────────────────────────────────────────────────────────────

def _render_exam_table(exams: list, lecturer_id: int) -> None:
    """
    Render the exam list as a styled column table.
    Columns: Course Code | Title | Status | Questions | Actions
    """
    # ── Table header row ──────────────────────────────────────────────────────
    h1, h2, h3, h4, h5 = st.columns([1.5, 3, 1.5, 1, 3])
    header_style = "font-weight:600; color:#555; font-size:0.8rem; text-transform:uppercase; letter-spacing:0.04em;"

    h1.markdown(f"<span style='{header_style}'>Course Code</span>", unsafe_allow_html=True)
    h2.markdown(f"<span style='{header_style}'>Title</span>", unsafe_allow_html=True)
    h3.markdown(f"<span style='{header_style}'>Status</span>", unsafe_allow_html=True)
    h4.markdown(f"<span style='{header_style}'>Q's</span>", unsafe_allow_html=True)
    h5.markdown(f"<span style='{header_style}'>Actions</span>", unsafe_allow_html=True)

    st.markdown(
        "<hr style='margin: 6px 0 0 0; border: none; border-top: 1px solid #E0E0E0;'>",
        unsafe_allow_html=True,
    )

    # ── Data rows ─────────────────────────────────────────────────────────────
    for exam in exams:
        _render_exam_row(exam, lecturer_id)
        st.markdown(
            "<hr style='margin: 4px 0; border: none; border-top: 1px solid #F0F0F0;'>",
            unsafe_allow_html=True,
        )


def _render_exam_row(exam: dict, lecturer_id: int) -> None:
    """
    Render one exam as a table row with status badge and conditional actions.

    Action availability by status:
      Draft     → Edit Questions | Publish Exam to Students (only if chief_approved)
      Published → Edit Questions | Close Exam
      Closed    → Edit Questions  (read-only review only)
    """
    exam_id: int = exam["id"]
    status: str = exam.get("status", "Draft")
    chief_approved: bool = bool(exam.get("chief_approved", False))

    # ── Question count (N+1 in Phase 2 — optimise with JOIN in Phase 3) ───────
    question_count = _get_question_count(exam_id)

    # ── Status badge HTML ─────────────────────────────────────────────────────
    badge_colour = _STATUS_COLOUR.get(status, "#757575")
    status_badge = (
        f"<span style='"
        f"background:{badge_colour}; color:white; "
        f"padding:2px 10px; border-radius:12px; font-size:0.75rem; font-weight:600;"
        f"'>{status}</span>"
    )

    # ── Row columns ───────────────────────────────────────────────────────────
    c_code, c_title, c_status, c_qcount, c_actions = st.columns([1.5, 3, 1.5, 1, 3])

    with c_code:
        st.markdown(
            f"<p style='margin:10px 0; font-weight:600; color:#004D40;'>"
            f"{exam.get('course_code', '—')}</p>",
            unsafe_allow_html=True,
        )

    with c_title:
        # Truncate long titles to keep the row compact.
        raw_title: str = exam.get("title", "—")
        display_title = raw_title if len(raw_title) <= 40 else raw_title[:37] + "…"
        st.markdown(
            f"<p style='margin:10px 0; color:#333;' title='{raw_title}'>"
            f"{display_title}</p>",
            unsafe_allow_html=True,
        )

    with c_status:
        st.markdown(
            f"<p style='margin:10px 0;'>{status_badge}</p>",
            unsafe_allow_html=True,
        )

    with c_qcount:
        st.markdown(
            f"<p style='margin:10px 0; text-align:center; color:#555;'>"
            f"{question_count}</p>",
            unsafe_allow_html=True,
        )

    with c_actions:
        # Stack action buttons vertically within the actions cell.
        # Unique keys per exam prevent Streamlit widget key collisions.

        # ── Edit Questions (always available) ─────────────────────────────────
        if st.button(
            "✏️ Edit Questions",
            key=f"edit_{exam_id}",
            use_container_width=True,
        ):
            st.session_state.selected_exam_id = exam_id
            st.session_state.staff_page = "question_editor"
            st.rerun()

        # ── Publish Exam to Students ──────────────────────────────────────────
        # Per spec: only shown when chief_approved == True AND status == 'Draft'.
        # Chief Examiner sets chief_approved via pages/chief_examiner/approval.py
        # (Phase 3). Until then this button is absent for all Draft exams.
        if status == "Draft" and chief_approved:
            if st.button(
                "🚀 Publish Exam to Students",
                key=f"publish_{exam_id}",
                use_container_width=True,
                type="primary",
            ):
                _handle_publish(exam_id, lecturer_id)

        # Show a disabled hint when the exam is a Draft awaiting CE approval.
        if status == "Draft" and not chief_approved:
            st.markdown(
                "<p style='font-size:0.75rem; color:#999; margin:2px 0 6px 0;'>"
                "⏳ Awaiting Chief Examiner approval to publish</p>",
                unsafe_allow_html=True,
            )

        # ── Close Exam ────────────────────────────────────────────────────────
        if status == "Published":
            if st.button(
                "🔒 Close Exam",
                key=f"close_{exam_id}",
                use_container_width=True,
            ):
                _handle_close(exam_id, lecturer_id)


def _get_question_count(exam_id: int) -> str:
    """
    Return the number of questions for an exam as a string.
    Returns '?' on any failure so a single broken exam doesn't crash the table.
    Phase 3 optimisation: replace N+1 calls with a single COUNT(*) GROUP BY
    query inside exam_repo.get_exams_by_lecturer().
    """
    try:
        questions = get_questions_by_exam(exam_id)
        return str(len(questions))
    except Exception as exc:
        log_to_audit(
            action="Question count fetch failure",
            exam_id=exam_id,
            details={"error": str(exc)},
        )
        return "?"


# ──────────────────────────────────────────────────────────────────────────────
# STATUS TRANSITION HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

def _handle_publish(exam_id: int, lecturer_id: int) -> None:
    """
    Transition exam from Draft → Published.
    Only callable when chief_approved == True (enforced in _render_exam_row).
    """
    try:
        update_exam_status(exam_id, "Published")
        log_to_audit(
            action="Exam published",
            user_id=str(lecturer_id),
            exam_id=exam_id,
            details={"new_status": "Published"},
        )
        st.success("✅ Exam published. Students can now see and start it.")
        st.rerun()
    except Exception as exc:
        log_to_audit(
            action="Exam publish failure",
            user_id=str(lecturer_id),
            exam_id=exam_id,
            details={"error": str(exc)},
        )
        st.error(f"Could not publish exam — {exc}")


def _handle_close(exam_id: int, lecturer_id: int) -> None:
    """
    Transition exam from Published → Closed.
    Closed exams are no longer joinable by students; existing sessions that are
    still 'In Progress' will be auto-submitted by the timer on next student load.
    """
    try:
        update_exam_status(exam_id, "Closed")
        log_to_audit(
            action="Exam closed",
            user_id=str(lecturer_id),
            exam_id=exam_id,
            details={"new_status": "Closed"},
        )
        st.success("🔒 Exam closed. No new submissions will be accepted.")
        st.rerun()
    except Exception as exc:
        log_to_audit(
            action="Exam close failure",
            user_id=str(lecturer_id),
            exam_id=exam_id,
            details={"error": str(exc)},
        )
        st.error(f"Could not close exam — {exc}")
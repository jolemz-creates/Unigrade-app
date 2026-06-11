# pages/lecturer/question_editor.py
# UniGrade — Lecturer Question Editor
# Hierarchical CRUD for exam questions (top-level and sub-questions).
# Displays Q1 / Q1a / Q1b nesting with indented cards, points summary,
# and add/edit/delete flows.
#
# Architecture note — _update_question_sql():
#   exam_repo.py (Phase 1C) has no update_question() function. Rather than
#   silently rewriting a completed Phase 1 file, _update_question_sql() below
#   issues a single parameterised UPDATE via get_connection().
#   Phase 3 action: add update_question() to models/exam_repo.py and replace
#   the call here with exam_repo.update_question(...).

import streamlit as st
from streamlit_quill import st_quill

from models.db_manager import get_connection
from models.exam_repo import (
    create_question,
    delete_question,
    get_exam_by_id,
    get_question_by_id,
    get_questions_by_exam,
)
from services.audit import log_to_audit
from services.sanitizer import strip_html_tags


# Lecturer Quill toolbar — NO tables (CLAUDE.md §7.2, ARCHITECT.md §2)
_LECTURER_TOOLBAR = ["bold", "italic", "underline", "bullet", "number"]

# Max characters shown in plain-text preview inside a question card
_PREVIEW_LEN = 90


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def render_question_editor() -> None:
    """
    Main entry point for the question editor.
    Called from app.py when st.session_state.staff_page == 'question_editor'.
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
    st.session_state.setdefault("show_add_question_form", False)
    st.session_state.setdefault("editing_question_id", None)
    st.session_state.setdefault("confirm_delete_question_id", None)

    # ── Guard: must have a selected exam ──────────────────────────────────────
    exam_id = st.session_state.selected_exam_id
    if not exam_id:
        st.warning("No exam selected. Returning to dashboard.")
        st.session_state.staff_page = "dashboard"
        st.rerun()
        return

    # ── Load exam metadata ────────────────────────────────────────────────────
    exam = get_exam_by_id(exam_id)
    if exam is None:
        st.error("Exam not found.")
        st.session_state.staff_page = "dashboard"
        st.session_state.selected_exam_id = None
        st.rerun()
        return

    # ── Load questions ────────────────────────────────────────────────────────
    try:
        questions = get_questions_by_exam(exam_id)
    except Exception as exc:
        log_to_audit(
            action="Question list load failure",
            user_id=str(st.session_state.user_id),
            exam_id=exam_id,
            details={"error": str(exc)},
        )
        st.error("Could not load questions. Please refresh.")
        return

    top_level, children_map = _build_hierarchy(questions)

    # ── Page header ───────────────────────────────────────────────────────────
    _render_editor_header(exam, questions)

    st.divider()

    # ── Add / Edit form area ──────────────────────────────────────────────────
    # Edit mode takes priority; the two forms are mutually exclusive.
    if st.session_state.editing_question_id is not None:
        _render_edit_question_form(exam_id, top_level)
        st.divider()

    else:
        # Add-form toggle (only when not editing)
        _, btn_col = st.columns([5, 2])
        with btn_col:
            toggle_label = (
                "✕ Cancel"
                if st.session_state.show_add_question_form
                else "＋ Add Question"
            )
            if st.button(toggle_label, use_container_width=True, key="toggle_add_form"):
                st.session_state.show_add_question_form = (
                    not st.session_state.show_add_question_form
                )
                st.rerun()

        if st.session_state.show_add_question_form:
            _render_add_question_form(exam_id, top_level)
            st.divider()

    # ── Question list ─────────────────────────────────────────────────────────
    if not questions:
        st.info("No questions yet. Click **＋ Add Question** to get started.")
    else:
        _render_question_list(top_level, children_map, exam_id)

    # ── Points summary ────────────────────────────────────────────────────────
    if questions:
        _render_points_summary(questions)


# ──────────────────────────────────────────────────────────────────────────────
# HEADER
# ──────────────────────────────────────────────────────────────────────────────

def _render_editor_header(exam: dict, questions: list) -> None:
    """Back-to-dashboard link, exam title, status badge, and metadata."""
    status = exam.get("status", "Draft")
    status_colours = {
        "Draft":     "#757575",
        "Published": "#004D40",
        "Closed":    "#E65100",
    }
    badge_colour = status_colours.get(status, "#757575")

    back_col, info_col = st.columns([1, 5])

    with back_col:
        if st.button("← Dashboard", key="back_to_dashboard"):
            st.session_state.staff_page = "dashboard"
            st.session_state.show_add_question_form = False
            st.session_state.editing_question_id = None
            st.session_state.confirm_delete_question_id = None
            st.rerun()

    with info_col:
        total_marks = sum(q.get("max_points", 0) for q in questions)
        st.markdown(
            f"""
            <div style="padding:4px 0;">
                <h2 style="color:#004D40; margin:0 0 4px 0; font-size:1.25rem;">
                    {exam.get('course_code', '')} — {exam.get('title', '')}
                </h2>
                <span style="
                    background:{badge_colour}; color:white;
                    padding:2px 10px; border-radius:12px;
                    font-size:0.72rem; font-weight:600; margin-right:10px;
                ">{status}</span>
                <span style="color:#777; font-size:0.82rem;">
                    {exam.get('duration', 0)} min
                    &nbsp;·&nbsp; {len(questions)} question{'s' if len(questions) != 1 else ''}
                    &nbsp;·&nbsp; {total_marks} total mark{'s' if total_marks != 1 else ''}
                </span>
            </div>
            """,
            unsafe_allow_html=True,
        )


# ──────────────────────────────────────────────────────────────────────────────
# HIERARCHY BUILDER
# ──────────────────────────────────────────────────────────────────────────────

def _build_hierarchy(questions: list) -> tuple:
    """
    Split a flat question list into top-level questions and a children map.
    Returns (top_level_list, {parent_id: [child_q, ...]}).
    """
    top_level = []
    children_map = {}

    for q in questions:
        pid = q.get("parent_question_id")
        if pid is None:
            top_level.append(q)
        else:
            children_map.setdefault(pid, []).append(q)

    return top_level, children_map


# ──────────────────────────────────────────────────────────────────────────────
# QUESTION LIST
# ──────────────────────────────────────────────────────────────────────────────

def _render_question_list(
    top_level: list,
    children_map: dict,
    exam_id: int,
) -> None:
    """Render full hierarchy: each parent followed by its indented children."""
    st.markdown(
        "<h4 style='color:#333; margin-bottom:10px;'>Questions</h4>",
        unsafe_allow_html=True,
    )

    for parent_q in top_level:
        pid = parent_q["id"]
        children = children_map.get(pid, [])

        _render_question_card(
            q=parent_q,
            is_child=False,
            has_children=bool(children),
            exam_id=exam_id,
        )

        for child_q in children:
            _render_question_card(
                q=child_q,
                is_child=True,
                has_children=False,   # Sub-questions cannot have further children
                exam_id=exam_id,
            )


def _render_question_card(
    q: dict,
    is_child: bool,
    has_children: bool,
    exam_id: int,
) -> None:
    """
    Render one question card with plain-text preview, model-answer expander,
    and Edit / Delete action buttons.
    Children are visually indented via a left-border colour shift and margin.
    """
    qid: int = q["id"]
    qnum: str = q.get("question_number", "?")
    max_pts: int = q.get("max_points", 0)
    required: bool = bool(q.get("is_required", True))
    confirming_delete: bool = (st.session_state.confirm_delete_question_id == qid)

    # Plain-text preview (strips Quill HTML)
    plain_preview = strip_html_tags(q.get("question_text", "")).strip()
    if len(plain_preview) > _PREVIEW_LEN:
        plain_preview = plain_preview[:_PREVIEW_LEN] + "…"

    indent_style  = "margin-left:28px;" if is_child else ""
    border_colour = "#80CBC4" if is_child else "#004D40"

    child_label = (
        "<span style='font-size:0.7rem;color:#777;margin-left:6px;'>sub-q</span>"
        if is_child else ""
    )
    req_label = (
        "<span style='font-size:0.7rem;color:#D32F2F;margin-left:6px;"
        "font-weight:600;'>[req]</span>"
        if required
        else "<span style='font-size:0.7rem;color:#999;margin-left:6px;'>[opt]</span>"
    )

    st.markdown(
        f"<div style='"
        f"background:white;border-radius:8px;padding:14px 18px 10px 18px;"
        f"margin-bottom:8px;box-shadow:0 2px 8px rgba(0,0,0,0.06);"
        f"border-left:3px solid {border_colour};{indent_style}'>",
        unsafe_allow_html=True,
    )

    hdr_col, act_col = st.columns([5, 2])

    with hdr_col:
        st.markdown(
            f"<p style='margin:0;font-weight:600;font-size:0.95rem;color:#004D40;'>"
            f"Q{qnum}{child_label}{req_label}"
            f"<span style='font-weight:400;color:#666;font-size:0.82rem;"
            f"margin-left:10px;'>{max_pts} mark{'s' if max_pts != 1 else ''}</span>"
            f"</p>",
            unsafe_allow_html=True,
        )

    with act_col:
        btn_edit_col, btn_del_col = st.columns(2)

        with btn_edit_col:
            if st.button("✏️ Edit", key=f"edit_q_{qid}", use_container_width=True):
                _enter_edit_mode(qid)

        with btn_del_col:
            if not confirming_delete:
                if st.button("🗑 Delete", key=f"del_q_{qid}", use_container_width=True):
                    st.session_state.confirm_delete_question_id = qid
                    st.session_state.show_add_question_form = False
                    st.session_state.editing_question_id = None
                    st.rerun()

    if plain_preview:
        st.markdown(
            f"<p style='margin:6px 0 4px 0;color:#444;font-size:0.88rem;'>"
            f"{plain_preview}</p>",
            unsafe_allow_html=True,
        )

    # ── Delete confirmation (two-step) ─────────────────────────────────────
    if confirming_delete:
        if has_children:
            st.warning(
                f"⚠️ Deleting **Q{qnum}** will also delete all its sub-questions. "
                f"This cannot be undone."
            )
        else:
            st.warning(f"⚠️ Delete **Q{qnum}**? This cannot be undone.")

        conf_col, cncl_col = st.columns(2)
        with conf_col:
            if st.button(
                "Yes, Delete",
                key=f"confirm_del_{qid}",
                type="primary",
                use_container_width=True,
            ):
                _handle_delete(qid, has_children, exam_id)
        with cncl_col:
            if st.button(
                "Cancel",
                key=f"cancel_del_{qid}",
                use_container_width=True,
            ):
                st.session_state.confirm_delete_question_id = None
                st.rerun()

    # ── Model answer + rubric expander ───────────────────────────────────────
    with st.expander("📋 Model Answer & Rubric", expanded=False):
        st.markdown("**Model Answer:**")
        st.markdown(q.get("model_answer", "—"), unsafe_allow_html=True)
        st.markdown("**Rubric:**")
        st.markdown(
            f"<pre style='font-size:0.82rem;white-space:pre-wrap;color:#444;'>"
            f"{q.get('rubric', '—')}</pre>",
            unsafe_allow_html=True,
        )

    st.markdown("</div>", unsafe_allow_html=True)


# ──────────────────────────────────────────────────────────────────────────────
# ADD QUESTION FORM
# ──────────────────────────────────────────────────────────────────────────────

def _render_add_question_form(exam_id: int, top_level_questions: list) -> None:
    """
    Form for adding a new question.
    Does NOT use st.form() — st_quill does not flush correctly inside a
    Streamlit form context.
    """
    st.markdown(
        "<h4 style='color:#004D40;margin-bottom:12px;'>Add New Question</h4>",
        unsafe_allow_html=True,
    )

    col_num, col_pts = st.columns([2, 1])
    with col_num:
        question_number = st.text_input(
            "Question Number *",
            key="new_q_number",
            placeholder="e.g. 1, 1a, 2b",
        )
    with col_pts:
        max_points = st.number_input(
            "Max Marks *", min_value=1, max_value=100, value=10, step=1,
            key="new_q_max_points",
        )

    col_req, col_parent = st.columns([1, 2])
    with col_req:
        is_required = st.checkbox("Required question", value=True, key="new_q_required")
    with col_parent:
        parent_id = _render_parent_selectbox(
            top_level_questions,
            selectbox_key="new_q_parent_label",
            current_question_id=None,
        )

    st.markdown(
        "<p style='font-size:0.85rem;color:#555;margin:10px 0 4px 0;'>"
        "Question Text *:</p>", unsafe_allow_html=True,
    )
    new_q_text_out = st_quill(
        placeholder="Enter the question for students...",
        key="new_q_text",
        toolbar=_LECTURER_TOOLBAR,
    )
    new_q_text: str = (
        new_q_text_out if new_q_text_out is not None
        else st.session_state.get("new_q_text", "")
    )

    st.markdown(
        "<p style='font-size:0.85rem;color:#555;margin:10px 0 4px 0;'>"
        "Model Answer *:</p>", unsafe_allow_html=True,
    )
    new_q_model_out = st_quill(
        placeholder="Enter the ideal answer for AI grading reference...",
        key="new_q_model_answer",
        toolbar=_LECTURER_TOOLBAR,
    )
    new_q_model: str = (
        new_q_model_out if new_q_model_out is not None
        else st.session_state.get("new_q_model_answer", "")
    )

    st.markdown(
        "<p style='font-size:0.85rem;color:#555;margin:10px 0 4px 0;'>"
        "Rubric / Marking Scheme *:</p>", unsafe_allow_html=True,
    )
    rubric = st.text_area(
        label="Rubric", label_visibility="collapsed",
        placeholder=(
            "e.g. 2 marks for definition, 3 marks for example, "
            "5 marks for explanation"
        ),
        height=100, key="new_q_rubric",
    )

    st.markdown("<br>", unsafe_allow_html=True)
    _, save_col = st.columns([4, 1])
    with save_col:
        if st.button(
            "＋ Add Question", type="primary",
            use_container_width=True, key="add_question_btn",
        ):
            _handle_add_question(
                exam_id=exam_id,
                question_number=question_number,
                question_text=new_q_text,
                model_answer=new_q_model,
                rubric=rubric,
                max_points=int(max_points),
                is_required=is_required,
                parent_question_id=parent_id,
            )


def _handle_add_question(
    exam_id: int,
    question_number: str,
    question_text: str,
    model_answer: str,
    rubric: str,
    max_points: int,
    is_required: bool,
    parent_question_id,
) -> None:
    """Validate, persist, audit, and reset the add form."""
    errors = []
    if not question_number.strip():
        errors.append("Question Number is required (e.g. 1, 1a, 2b).")
    if not strip_html_tags(question_text).strip():
        errors.append("Question Text is required.")
    if not strip_html_tags(model_answer).strip():
        errors.append("Model Answer is required.")
    if not rubric.strip():
        errors.append("Rubric / Marking Scheme is required.")

    if errors:
        for msg in errors:
            st.error(msg)
        return

    try:
        new_id = create_question(
            exam_id=exam_id,
            parent_question_id=parent_question_id,
            question_number=question_number.strip(),
            question_text=question_text,
            model_answer=model_answer,
            rubric=rubric.strip(),
            max_points=max_points,
            is_required=is_required,
        )
    except Exception as exc:
        log_to_audit(
            action="Question creation failure",
            user_id=str(st.session_state.user_id),
            exam_id=exam_id,
            details={"error": str(exc), "question_number": question_number},
        )
        st.error(f"Could not add question — {exc}. Your inputs are preserved.")
        return

    log_to_audit(
        action=f"Question created: Q{question_number.strip()}",
        user_id=str(st.session_state.user_id),
        exam_id=exam_id,
        details={"question_id": new_id, "max_points": max_points},
    )

    for key in (
        "new_q_number", "new_q_max_points", "new_q_required",
        "new_q_parent_label", "new_q_text", "new_q_model_answer", "new_q_rubric",
    ):
        st.session_state.pop(key, None)

    st.session_state.show_add_question_form = False
    st.success(f"✅ Q{question_number.strip()} added successfully.")
    st.rerun()


# ──────────────────────────────────────────────────────────────────────────────
# EDIT QUESTION FORM
# ──────────────────────────────────────────────────────────────────────────────

def _enter_edit_mode(question_id: int) -> None:
    """
    Load a question from the DB into edit_q_* session state keys and switch
    to edit mode. Setting the Quill keys before the next render means
    st_quill will initialise with the existing HTML content.
    """
    q = get_question_by_id(question_id)
    if q is None:
        st.error("Question not found — it may have been deleted.")
        st.rerun()
        return

    st.session_state["edit_q_number"]       = q["question_number"]
    st.session_state["edit_q_text"]         = q.get("question_text", "")
    st.session_state["edit_q_model_answer"] = q.get("model_answer", "")
    st.session_state["edit_q_rubric"]       = q.get("rubric", "")
    st.session_state["edit_q_max_points"]   = q.get("max_points", 1)
    st.session_state["edit_q_required"]     = bool(q.get("is_required", True))
    st.session_state["edit_q_parent_id"]    = q.get("parent_question_id")

    st.session_state.editing_question_id       = question_id
    st.session_state.show_add_question_form    = False
    st.session_state.confirm_delete_question_id = None
    st.rerun()


def _render_edit_question_form(exam_id: int, top_level_questions: list) -> None:
    """
    Edit form pre-populated from edit_q_* session state keys.
    Saves via _update_question_sql().
    Phase 3: replace _update_question_sql() call with exam_repo.update_question().
    """
    editing_id: int = st.session_state.editing_question_id

    st.markdown(
        "<h4 style='color:#004D40;margin-bottom:12px;'>✏️ Edit Question</h4>",
        unsafe_allow_html=True,
    )

    col_num, col_pts = st.columns([2, 1])
    with col_num:
        question_number = st.text_input(
            "Question Number *", key="edit_q_number",
        )
    with col_pts:
        max_points = st.number_input(
            "Max Marks *", min_value=1, max_value=100, step=1,
            key="edit_q_max_points",
        )

    col_req, col_parent = st.columns([1, 2])
    with col_req:
        is_required = st.checkbox("Required question", key="edit_q_required")
    with col_parent:
        # Exclude the question being edited from the parent dropdown
        eligible_parents = [q for q in top_level_questions if q["id"] != editing_id]
        saved_parent_id  = st.session_state.get("edit_q_parent_id")
        parent_id = _render_parent_selectbox(
            eligible_parents,
            selectbox_key="edit_q_parent_label",
            current_question_id=editing_id,
            preselected_parent_id=saved_parent_id,
        )

    st.markdown(
        "<p style='font-size:0.85rem;color:#555;margin:10px 0 4px 0;'>"
        "Question Text *:</p>", unsafe_allow_html=True,
    )
    edit_q_text_out = st_quill(
        value=st.session_state.get("edit_q_text", ""),
        key="edit_q_text",
        toolbar=_LECTURER_TOOLBAR,
    )
    edit_q_text: str = (
        edit_q_text_out if edit_q_text_out is not None
        else st.session_state.get("edit_q_text", "")
    )

    st.markdown(
        "<p style='font-size:0.85rem;color:#555;margin:10px 0 4px 0;'>"
        "Model Answer *:</p>", unsafe_allow_html=True,
    )
    edit_model_out = st_quill(
        value=st.session_state.get("edit_q_model_answer", ""),
        key="edit_q_model_answer",
        toolbar=_LECTURER_TOOLBAR,
    )
    edit_model: str = (
        edit_model_out if edit_model_out is not None
        else st.session_state.get("edit_q_model_answer", "")
    )

    st.markdown(
        "<p style='font-size:0.85rem;color:#555;margin:10px 0 4px 0;'>"
        "Rubric / Marking Scheme *:</p>", unsafe_allow_html=True,
    )
    rubric = st.text_area(
        label="Rubric", label_visibility="collapsed",
        height=100, key="edit_q_rubric",
    )

    st.markdown("<br>", unsafe_allow_html=True)
    save_col, cancel_col, _ = st.columns([1, 1, 3])

    with save_col:
        if st.button(
            "💾 Save Changes", type="primary",
            use_container_width=True, key="save_edit_btn",
        ):
            _handle_edit_question(
                exam_id=exam_id,
                question_id=editing_id,
                question_number=question_number,
                question_text=edit_q_text,
                model_answer=edit_model,
                rubric=rubric,
                max_points=int(max_points),
                is_required=is_required,
                parent_question_id=parent_id,
            )

    with cancel_col:
        if st.button(
            "✕ Cancel", use_container_width=True, key="cancel_edit_btn",
        ):
            _clear_edit_state()
            st.rerun()


def _handle_edit_question(
    exam_id: int,
    question_id: int,
    question_number: str,
    question_text: str,
    model_answer: str,
    rubric: str,
    max_points: int,
    is_required: bool,
    parent_question_id,
) -> None:
    """Validate, update via SQL, audit, and clear edit state."""
    errors = []
    if not question_number.strip():
        errors.append("Question Number is required.")
    if not strip_html_tags(question_text).strip():
        errors.append("Question Text is required.")
    if not strip_html_tags(model_answer).strip():
        errors.append("Model Answer is required.")
    if not rubric.strip():
        errors.append("Rubric is required.")

    if errors:
        for msg in errors:
            st.error(msg)
        return

    try:
        _update_question_sql(
            question_id=question_id,
            question_number=question_number.strip(),
            question_text=question_text,
            model_answer=model_answer,
            rubric=rubric.strip(),
            max_points=max_points,
            is_required=is_required,
            parent_question_id=parent_question_id,
        )
    except Exception as exc:
        log_to_audit(
            action="Question update failure",
            user_id=str(st.session_state.user_id),
            exam_id=exam_id,
            details={"error": str(exc), "question_id": question_id},
        )
        st.error(f"Could not save changes — {exc}. Inputs are preserved.")
        return

    log_to_audit(
        action=f"Question updated: Q{question_number.strip()}",
        user_id=str(st.session_state.user_id),
        exam_id=exam_id,
        details={"question_id": question_id, "max_points": max_points},
    )

    _clear_edit_state()
    st.success(f"✅ Q{question_number.strip()} updated.")
    st.rerun()


def _clear_edit_state() -> None:
    """Remove all edit_q_* session state keys and reset editing_question_id."""
    for key in (
        "edit_q_number", "edit_q_text", "edit_q_model_answer",
        "edit_q_rubric", "edit_q_max_points", "edit_q_required",
        "edit_q_parent_id", "edit_q_parent_label",
    ):
        st.session_state.pop(key, None)
    st.session_state.editing_question_id = None


# ──────────────────────────────────────────────────────────────────────────────
# DELETE HANDLER
# ──────────────────────────────────────────────────────────────────────────────

def _handle_delete(question_id: int, has_children: bool, exam_id: int) -> None:
    """
    Delete a question. DB FK ON DELETE CASCADE removes child questions
    automatically. Audit log is written before deletion so a record survives
    even if the UI doesn't rerender cleanly.
    """
    try:
        q = get_question_by_id(question_id)
        q_num = q["question_number"] if q else str(question_id)

        log_to_audit(
            action=(
                f"Question deleted: Q{q_num}"
                + (" (cascade: sub-questions removed)" if has_children else "")
            ),
            user_id=str(st.session_state.user_id),
            exam_id=exam_id,
            details={"question_id": question_id, "had_children": has_children},
        )

        delete_question(question_id)

    except Exception as exc:
        log_to_audit(
            action="Question delete failure",
            user_id=str(st.session_state.user_id),
            exam_id=exam_id,
            details={"error": str(exc), "question_id": question_id},
        )
        st.session_state.confirm_delete_question_id = None
        st.error(f"Could not delete question — {exc}")
        st.rerun()
        return

    st.session_state.confirm_delete_question_id = None
    st.rerun()


# ──────────────────────────────────────────────────────────────────────────────
# PARENT SELECTBOX HELPER
# ──────────────────────────────────────────────────────────────────────────────

def _render_parent_selectbox(
    top_level_questions: list,
    selectbox_key: str,
    current_question_id,
    preselected_parent_id=None,
) -> object:
    """
    Render a selectbox for choosing a parent question (or None = top-level).
    Returns the selected parent question ID, or None.

    current_question_id  — excluded from options (self-referential guard)
    preselected_parent_id — used in edit mode to restore the stored parent
    """
    _NONE_LABEL = "None (Top-level Question)"

    option_labels = [_NONE_LABEL]
    option_ids    = [None]

    for q in top_level_questions:
        if q["id"] == current_question_id:
            continue
        preview = strip_html_tags(q.get("question_text", "")).strip()
        truncated = preview[:35] + "…" if len(preview) > 35 else preview
        option_labels.append(f"Q{q['question_number']} — {truncated}")
        option_ids.append(q["id"])

    default_idx = 0
    if preselected_parent_id is not None:
        for i, oid in enumerate(option_ids):
            if oid == preselected_parent_id:
                default_idx = i
                break

    selected_label = st.selectbox(
        "Parent Question",
        options=option_labels,
        index=default_idx,
        key=selectbox_key,
    )

    return option_ids[option_labels.index(selected_label)]


# ──────────────────────────────────────────────────────────────────────────────
# POINTS SUMMARY
# ──────────────────────────────────────────────────────────────────────────────

def _render_points_summary(questions: list) -> None:
    """Show total marks, main question count, and sub-question count at the bottom."""
    total_marks = sum(q.get("max_points", 0) for q in questions)
    top_count   = sum(1 for q in questions if q.get("parent_question_id") is None)
    sub_count   = len(questions) - top_count

    parts = [
        f"**{total_marks}** total mark{'s' if total_marks != 1 else ''}",
        f"**{top_count}** main question{'s' if top_count != 1 else ''}",
    ]
    if sub_count:
        parts.append(
            f"**{sub_count}** sub-question{'s' if sub_count != 1 else ''}"
        )

    st.divider()
    st.markdown(
        "<p style='color:#555;font-size:0.88rem;'>📊 &nbsp;"
        + " &nbsp;·&nbsp; ".join(parts) + "</p>",
        unsafe_allow_html=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
# SQL UPDATE HELPER
# Phase 3 action: move this into models/exam_repo.py as update_question() and
# import it here instead. The function signature and SQL are stable — the
# migration is a copy-paste + import swap with no logic changes.
# ──────────────────────────────────────────────────────────────────────────────

def _update_question_sql(
    question_id: int,
    question_number: str,
    question_text: str,
    model_answer: str,
    rubric: str,
    max_points: int,
    is_required: bool,
    parent_question_id,
) -> None:
    """
    Parameterised UPDATE on the questions table.
    All inputs are validated by _handle_edit_question() before this is called.
    Uses get_connection() directly — see module docstring for rationale.
    """
    sql = """
        UPDATE questions
        SET  question_number    = ?,
             question_text      = ?,
             model_answer       = ?,
             rubric             = ?,
             max_points         = ?,
             is_required        = ?,
             parent_question_id = ?
        WHERE id = ?
    """
    with get_connection() as conn:
        conn.execute(
            sql,
            (
                question_number,
                question_text,
                model_answer,
                rubric,
                max_points,
                int(is_required),
                parent_question_id,
                question_id,
            ),
        )
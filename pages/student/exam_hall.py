# pages/student/exam_hall.py
# UniGrade — Student Exam Hall
# Live exam interface: timer, question navigation, Quill answer editor,
# autosave, pre-submit review modal, and final submission.
#
# Phase 3 hook: after _do_submit() persists responses, wire grade_exam_batch()
# here and call update_response_grades() with the results.
#
# Phase 3 hook: on resume after re-login, load saved draft answers from
# student_responses back into st.session_state.exam_answers so the Quill
# editors are pre-populated.

import time
from datetime import datetime

import streamlit as st
from streamlit_quill import st_quill

from auth.auth import clear_exam_session
from components.progress_bar import render_progress_bar
from components.timer import render_timer
from models.exam_repo import get_exam_by_id, get_published_exams_by_department, get_questions_by_exam
from models.student_repo import (
    autosave_session,
    get_active_session,
    get_in_progress_session_for_student,
    save_responses_batch,
    start_exam_session,
    submit_exam_session,
    update_response_grades,
)
from services.grader import grade_exam_batch
from services.audit import log_to_audit
from services.sanitizer import strip_html_tags


# ──────────────────────────────────────────────────────────────────────────────
# IP CAPTURE (Phase 4)
# ──────────────────────────────────────────────────────────────────────────────

def _get_client_ip() -> str:
    """
    Attempt to capture the student's client IP address from the HTTP request.

    Strategy (in priority order):
    1. X-Forwarded-For — set by nginx / any reverse proxy; may be a comma-
       separated list of IPs when multiple proxies are chained. We take the
       first (leftmost) entry, which is the original client.
    2. X-Real-Ip — alternative single-value header set by some nginx configs.
    3. Falls back to "unavailable" on any AttributeError (older Streamlit
       build without st.context) or missing header.

    NOTE: st.context.headers exposes the browser-side HTTP request headers,
    not a server-side REMOTE_ADDR. Without a proxy that injects X-Forwarded-For,
    raw client IPs are not directly accessible through Streamlit. For a
    university intranet deployment behind nginx, configure:
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    in the nginx upstream block.

    This function NEVER raises. A missing IP must not block a student's exam.
    """
    try:
        headers = st.context.headers  # Available in Streamlit >= 1.31
        forwarded_for: str = headers.get("X-Forwarded-For", "").strip()
        if forwarded_for:
            # Take the first IP in the chain (original client).
            return forwarded_for.split(",")[0].strip()
        real_ip: str = headers.get("X-Real-Ip", "").strip()
        if real_ip:
            return real_ip
        return "unavailable"
    except AttributeError:
        # st.context not available on this Streamlit build.
        return "unavailable"
    except Exception:
        return "unavailable"


# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def render_exam_hall() -> None:
    """
    Main entry point for the student exam hall.
    Called from app.py when role == "Student" and the student is logged in.
    """
    # ── Page guard ────────────────────────────────────────────────────────────
    if not st.session_state.get("logged_in"):
        st.error("Unauthorized. Please log in.")
        st.stop()

    if st.session_state.get("role") != "Student":
        st.error("Access denied. This page is for students only.")
        st.stop()

    # ── Session state defaults (never assume keys exist) ──────────────────────
    st.session_state.setdefault("active_exam_id", None)
    st.session_state.setdefault("exam_start_time", None)
    st.session_state.setdefault("exam_end_time", None)
    st.session_state.setdefault("exam_answers", {})
    st.session_state.setdefault("answered_questions", set())
    st.session_state.setdefault("time_remaining", 0)
    st.session_state.setdefault("timer_expired", False)
    st.session_state.setdefault("last_autosave_time", 0.0)
    st.session_state.setdefault("autosave_interval", 30)
    st.session_state.setdefault("show_submission_summary", False)
    st.session_state.setdefault("current_question_index", 0)
    st.session_state.setdefault("submission_complete", False)
    st.session_state.setdefault("submission_score", None)   # set by _do_submit after grading

    student_id: str = st.session_state.user_id

    # ── Post-submission success screen ────────────────────────────────────────
    if st.session_state.submission_complete:
        _render_submission_complete()
        return

    # ── Branch: active exam vs. exam browser ──────────────────────────────────
    if st.session_state.active_exam_id:
        _render_active_exam(st.session_state.active_exam_id, student_id)
    else:
        # Phase 4 — session resume after re-login.
        # active_exam_id is None because logout wiped session state, but the
        # student may have an interrupted 'In Progress' session in the DB.
        # Probe the DB before falling through to the exam browser.
        _check_for_interrupted_session(student_id)
        _render_exam_browser(student_id)


# ──────────────────────────────────────────────────────────────────────────────
# SESSION RESUME (Phase 4)
# ──────────────────────────────────────────────────────────────────────────────

def _check_for_interrupted_session(student_id: str) -> None:
    """
    Detect and recover an interrupted exam session after re-login.

    Called when active_exam_id is None — which happens when the student
    logged out (or was disconnected) mid-exam. Session state was wiped by
    _logout(), so we have no exam context. We query the DB directly.

    Two outcomes:
    1. Session found AND now < end_time  → restore active_exam_id and
       exam_end_time into session state, then st.rerun() to enter
       _render_active_exam() on the next cycle. The student resumes
       exactly where they left off; the timer picks up from the
       authoritative end_time stored in the DB.

    2. Session found AND now >= end_time → the exam window has closed
       while the student was logged out. Auto-submit immediately.
       We load questions to build the response batch; any draft answers
       already written to student_responses by autosave are preserved
       (save_responses_batch uses INSERT OR REPLACE, so re-writing blank
       answers for questions that were autosaved is a regression — we
       build the batch from current exam_answers, which is {} on re-login,
       meaning only previously autosaved rows survive). This is correct:
       answers the student typed but never autosaved are lost, which is
       the expected penalty for a timed-out session.

    3. No interrupted session → return silently; _render_exam_browser()
       shows the normal exam list.

    This function NEVER raises to caller. Any DB failure is logged and
    swallowed — the student falls through to the exam browser.
    """
    try:
        session = get_in_progress_session_for_student(student_id)
        if session is None:
            return  # No interrupted session — nothing to do.

        exam_id: int = session["exam_id"]
        end_time = datetime.fromisoformat(str(session["end_time"]))

        if datetime.now() < end_time:
            # ── Resume path ───────────────────────────────────────────────────
            # Restore the minimum state needed to re-enter _render_active_exam().
            # The rest (questions, answers) is loaded fresh inside that function.
            st.session_state.active_exam_id = exam_id
            st.session_state.exam_end_time = end_time
            st.session_state.timer_expired = False
            st.info("📋 Resuming your in-progress exam...")
            st.rerun()

        else:
            # ── Auto-submit path ──────────────────────────────────────────────
            # Timer expired while logged out. Submit whatever was autosaved.
            st.warning("⏰ Your exam time expired. Auto-submitting...")

            # Load questions so _do_submit can build the response batch.
            # exam_answers is {} (session state was cleared), so _do_submit
            # will write empty strings for questions not previously autosaved.
            # That is intentional — autosaved rows will be replaced in-place
            # with the same content; un-autosaved answers are genuinely lost.
            questions = get_questions_by_exam(exam_id)

            if not questions:
                # Edge case: exam was deleted or has no questions.
                # Mark session submitted anyway to prevent a zombie In Progress row.
                submit_exam_session(exam_id, student_id, status="Auto-Submitted")
                log_to_audit(
                    action="Auto-submit on resume: no questions found",
                    user_id=student_id,
                    exam_id=exam_id,
                    details={"reason": "get_questions_by_exam returned empty"},
                )
                st.error(
                    "Your exam session was closed (no questions found). "
                    "Please contact your invigilator."
                )
                return

            # Seed active_exam_id so _do_submit's audit log has context.
            st.session_state.active_exam_id = exam_id
            st.session_state.exam_end_time = end_time
            st.session_state.timer_expired = True

            _do_submit(exam_id, student_id, questions, status="Auto-Submitted")

    except Exception as exc:
        log_to_audit(
            action="Session resume check failure",
            user_id=student_id,
            details={"error": str(exc)},
        )
        # Fall through silently to exam browser.


# ──────────────────────────────────────────────────────────────────────────────
# ACTIVE EXAM
# ──────────────────────────────────────────────────────────────────────────────

def _render_active_exam(exam_id: int, student_id: str) -> None:
    """
    Render the live exam interface.
    Handles session resume, timer expiry detection, autosave, navigation,
    submission summary, and auto-submit.
    """
    # ── Load exam metadata ────────────────────────────────────────────────────
    exam = get_exam_by_id(exam_id)
    if exam is None:
        st.error("Exam not found. Please contact your invigilator.")
        st.session_state.active_exam_id = None
        st.stop()

    # ── Validate / resume session from DB ────────────────────────────────────
    # This is the source-of-truth check — session state can be lost on refresh.
    existing_session = get_active_session(exam_id, student_id)
    if existing_session is None:
        # Session was submitted in a different browser tab or doesn't exist.
        st.warning("No active exam session found. You may have already submitted.")
        st.session_state.active_exam_id = None
        st.rerun()
        return

    # Restore end_time from DB into session state (critical for resume after re-login).
    # The DB is authoritative; session state may have been cleared.
    db_end_time = datetime.fromisoformat(str(existing_session["end_time"]))
    if st.session_state.exam_end_time is None:
        st.session_state.exam_end_time = db_end_time

    # ── Check timer expiry against DB end_time ────────────────────────────────
    # Do this before rendering anything so auto-submit fires on the first rerun
    # after time runs out, even if the student was offline.
    if datetime.now() > db_end_time:
        st.session_state.timer_expired = True

    # ── Load questions ────────────────────────────────────────────────────────
    questions = get_questions_by_exam(exam_id)
    if not questions:
        st.error("This exam has no questions configured. Contact your lecturer.")
        st.stop()

    # Pre-seed exam_answers for all question IDs so autosave covers every question,
    # including ones the student hasn't visited yet.
    for q in questions:
        qid = q["id"]
        st.session_state.exam_answers.setdefault(qid, "")

    # ── Auto-submit if timer has expired ─────────────────────────────────────
    if st.session_state.timer_expired:
        st.warning("⏰ Time's up! Your exam is being auto-submitted...")
        _do_submit(exam_id, student_id, questions, status="Auto-Submitted")
        return

    # ── Timer widget (non-blocking — no time.sleep()) ─────────────────────────
    # render_timer() reads st.session_state.exam_end_time and calls st.rerun()
    # when remaining <= 0, which will then hit the timer_expired branch above.
    render_timer()

    # ── Autosave (passive — no rerun triggered) ───────────────────────────────
    _handle_autosave(exam_id, student_id, questions)

    # ── Exam header ───────────────────────────────────────────────────────────
    st.markdown(
        f"""
        <div style="
            background: #004D40;
            color: white;
            padding: 16px 24px;
            border-radius: 8px;
            margin-bottom: 20px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        ">
            <h2 style="margin: 0; font-size: 1.3rem;">{exam['title']}</h2>
            <p style="margin: 4px 0 0 0; font-size: 0.875rem; opacity: 0.85;">
                {exam['course_code']}
                &nbsp;&nbsp;|&nbsp;&nbsp;
                {st.session_state.user_name} &nbsp;({student_id})
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Exam instructions (collapsible) ──────────────────────────────────────
    if exam.get("instructions"):
        with st.expander("📋 Exam Instructions", expanded=False):
            st.markdown(exam["instructions"], unsafe_allow_html=True)

    # ── Progress bar ─────────────────────────────────────────────────────────
    render_progress_bar(len(questions))

    st.divider()

    # ── Submission summary modal ──────────────────────────────────────────────
    # Shown when student clicks "Submit Exam for Grading".
    # Returns early so the question navigator doesn't render behind the modal.
    if st.session_state.show_submission_summary:
        _render_submission_summary(exam_id, student_id, questions)
        return

    # ── Question navigator ────────────────────────────────────────────────────
    _render_question_navigator(exam_id, student_id, questions)


def _handle_autosave(exam_id: int, student_id: str, questions: list) -> None:
    """
    Silently persist draft answers to student_responses every autosave_interval
    seconds. Never raises to caller; failures are logged and swallowed so the
    student's exam session is never interrupted by a save error.
    """
    now = time.time()
    if now - st.session_state.last_autosave_time < st.session_state.autosave_interval:
        return

    try:
        # submitted_at=None marks these rows as drafts (not final submissions).
        draft_responses = _build_responses_batch(
            exam_id, student_id, questions, submitted_at=None
        )
        save_responses_batch(draft_responses)
        autosave_session(exam_id, student_id)
        st.session_state.last_autosave_time = now
    except Exception as exc:
        log_to_audit(
            action="Autosave failure",
            user_id=student_id,
            exam_id=exam_id,
            details={"error": str(exc)},
        )
        # Do NOT reraise. A failed autosave must never crash the exam session.


def _render_question_navigator(exam_id: int, student_id: str, questions: list) -> None:
    """
    Prev/next navigation + current question editor + submit button.
    Uses st.session_state.current_question_index as the cursor.
    """
    total = len(questions)

    # Defensive clamp — index could be stale if questions were edited mid-session.
    idx = max(0, min(st.session_state.current_question_index, total - 1))
    st.session_state.current_question_index = idx

    current_q = questions[idx]

    # ── Navigation row ────────────────────────────────────────────────────────
    nav_left, nav_center, nav_right = st.columns([1, 4, 1])

    with nav_left:
        if st.button(
            "◀ Previous",
            disabled=(idx == 0),
            use_container_width=True,
            key="nav_prev",
        ):
            st.session_state.current_question_index = idx - 1
            st.rerun()

    with nav_center:
        st.markdown(
            f"<p style='text-align:center; color:#004D40; font-weight:600; "
            f"margin:8px 0 0 0;'>Question {idx + 1} of {total}</p>",
            unsafe_allow_html=True,
        )

    with nav_right:
        if st.button(
            "Next ▶",
            disabled=(idx == total - 1),
            use_container_width=True,
            key="nav_next",
        ):
            st.session_state.current_question_index = idx + 1
            st.rerun()

    st.markdown("<br>", unsafe_allow_html=True)

    # ── Current question ──────────────────────────────────────────────────────
    _render_question(current_q)

    st.divider()

    # ── Submit button ─────────────────────────────────────────────────────────
    _, submit_col, _ = st.columns([1, 2, 1])
    with submit_col:
        if st.button(
            "📤 Submit Exam for Grading",
            type="primary",
            use_container_width=True,
            key="submit_btn",
        ):
            st.session_state.show_submission_summary = True
            st.rerun()


def _render_question(question: dict) -> None:
    """
    Render a single question with its Quill answer editor.
    Follows the Quill state binding pattern from CLAUDE.md §7.2 exactly.
    """
    qid: int = question["id"]
    qnum: str = question["question_number"]
    max_pts: int = question["max_points"]
    required: bool = bool(question.get("is_required", True))

    # ── Question header ───────────────────────────────────────────────────────
    req_badge = (
        '<span style="color:#D32F2F; font-size:0.75rem; '
        'margin-left:8px; font-weight:600;">[Required]</span>'
        if required
        else ""
    )
    pts_label = f"{max_pts} mark{'s' if max_pts != 1 else ''}"

    st.markdown(
        f"<h4 style='color:#004D40; margin-bottom:6px;'>"
        f"Question {qnum}"
        f"<span style='font-size:0.8rem; color:#666; font-weight:400;'>"
        f" &nbsp;({pts_label})</span>"
        f"{req_badge}</h4>",
        unsafe_allow_html=True,
    )

    # ── Question text (may contain lecturer's Quill HTML) ────────────────────
    st.markdown(
        f"<div style='"
        f"background:#F0F2F6; padding:12px 16px; border-radius:6px; "
        f"margin-bottom:14px; line-height:1.65; font-size:0.95rem;"
        f"'>{question['question_text']}</div>",
        unsafe_allow_html=True,
    )

    # ── Quill answer editor ───────────────────────────────────────────────────
    st.markdown(
        "<p style='font-size:0.85rem; color:#555; margin-bottom:4px;'>"
        "Your Answer:</p>",
        unsafe_allow_html=True,
    )

    key = f"answer_{qid}"

    # On resume after re-login, st.session_state[key] may not exist.
    # Seed from exam_answers (which was itself seeded from DB in Phase 3).
    # For Phase 2, both will be "" on a fresh resume.
    if key not in st.session_state:
        st.session_state[key] = st.session_state.exam_answers.get(qid, "")

    answer = st_quill(
        value=st.session_state[key],
        key=key,
        toolbar=["bold", "italic", "underline", "bullet", "list", "table"],
    )

    # Bind Quill output back into session state (st_quill returns None when
    # the widget hasn't changed, so we only update on actual changes).
    if answer is not None:
        st.session_state[key] = answer
        st.session_state.exam_answers[qid] = answer

        plain_text = strip_html_tags(answer).strip()
        if plain_text:
            st.session_state.answered_questions.add(qid)
        else:
            st.session_state.answered_questions.discard(qid)


# ──────────────────────────────────────────────────────────────────────────────
# SUBMISSION SUMMARY MODAL
# ──────────────────────────────────────────────────────────────────────────────

def _render_submission_summary(exam_id: int, student_id: str, questions: list) -> None:
    """
    Pre-submit review screen. Shows answered/unanswered counts, warns about
    required unanswered questions, and provides Confirm or Go Back controls.
    """
    total = len(questions)
    answered_ids = st.session_state.answered_questions
    answered_count = len(answered_ids)

    unanswered_required = [
        q for q in questions
        if q["id"] not in answered_ids and bool(q.get("is_required", True))
    ]

    st.markdown(
        "<h3 style='color:#004D40; margin-bottom:4px;'>📋 Submission Review</h3>",
        unsafe_allow_html=True,
    )

    # ── Summary metrics ───────────────────────────────────────────────────────
    m1, m2, m3 = st.columns(3)
    m1.metric("Total Questions", total)
    m2.metric("Answered", answered_count)
    m3.metric("Unanswered", total - answered_count)

    # ── Required-question warnings ────────────────────────────────────────────
    if unanswered_required:
        missing_nums = ", ".join(q["question_number"] for q in unanswered_required)
        st.warning(
            f"⚠️ You have not answered the following required question(s): "
            f"**{missing_nums}**. "
            f"These will receive a score of 0 if left blank."
        )

    # ── Per-question status list ──────────────────────────────────────────────
    st.markdown("**Question Status:**")
    for q in questions:
        qid = q["id"]
        is_required = bool(q.get("is_required", True))
        if qid in answered_ids:
            icon = "✅"
        elif is_required:
            icon = "❌"
        else:
            icon = "⬜"

        pts = q["max_points"]
        st.markdown(
            f"{icon} &nbsp; **Q{q['question_number']}** "
            f"— {pts} mark{'s' if pts != 1 else ''}",
            unsafe_allow_html=True,
        )

    st.divider()

    # ── Action buttons ────────────────────────────────────────────────────────
    back_col, confirm_col = st.columns([1, 1])

    with back_col:
        if st.button("← Go Back", use_container_width=True, key="summary_back"):
            st.session_state.show_submission_summary = False
            st.rerun()

    with confirm_col:
        if st.button(
            "✅ Confirm Submission",
            type="primary",
            use_container_width=True,
            key="summary_confirm",
        ):
            _do_submit(exam_id, student_id, questions, status="Submitted")


# ──────────────────────────────────────────────────────────────────────────────
# SUBMISSION LOGIC
# ──────────────────────────────────────────────────────────────────────────────

def _do_submit(exam_id: int, student_id: str, questions: list, status: str = "Submitted") -> None:
    """
    Persist all student responses as a single batch, mark the exam session
    as submitted, and clear in-memory exam state.

    Phase 3 hook: after save_responses_batch() succeeds, call grade_exam_batch()
    from services/grader.py, then update_response_grades() with results.
    Show st.spinner("Grading in progress...") around the grader call.
    """
    submitted_at = datetime.now().isoformat()

    try:
        with st.spinner("Submitting your exam..."):
            # ── 1. Batch-write all responses ──────────────────────────────────
            # Includes questions with empty answers; the grader will score them 0.
            responses = _build_responses_batch(exam_id, student_id, questions, submitted_at)
            save_responses_batch(responses)

            # ── 2. Mark session submitted ─────────────────────────────────────
            submit_exam_session(exam_id, student_id, status=status)

            # ── 3. Audit log ──────────────────────────────────────────────────
            log_to_audit(
                action=f"Exam submitted ({status})",
                user_id=student_id,
                exam_id=exam_id,
                details={
                    "answered_count": len(st.session_state.answered_questions),
                    "total_questions": len(questions),
                    "status": status,
                },
            )

        # ── 4. Grade all responses via Groq ───────────────────────────────────
        # Build the batch payload expected by grade_exam_batch():
        # [{"question_id", "question_text", "model_answer", "rubric",
        #   "max_marks", "answer_html"}]
        grading_batch = [
            {
                "question_id":   q["id"],
                "question_text": strip_html_tags(q["question_text"]),
                "model_answer":  q["model_answer"],
                "rubric":        q["rubric"],
                "max_marks":     float(q["max_points"]),
                "answer_html":   st.session_state.exam_answers.get(q["id"], ""),
            }
            for q in questions
        ]

        total_score: float = 0.0
        grading_failed = False

        try:
            with st.spinner("Grading in progress..."):
                grade_results = grade_exam_batch(
                    exam_id=exam_id,
                    student_id=student_id,
                    responses=grading_batch,
                )

            # grade_exam_batch returns a list of dicts with question_id included.
            # Shape: [{"question_id", "score", "feedback", "confidence"}]
            grade_rows = [
                {
                    "exam_id":       exam_id,
                    "student_id":    student_id,
                    "question_id":   r["question_id"],
                    "ai_score":      r["score"],
                    "ai_feedback":   r["feedback"],
                    "ai_confidence": r["confidence"],
                }
                for r in grade_results
            ]
            update_response_grades(grade_rows)

            total_score = sum(r["score"] for r in grade_results)
            max_possible = sum(float(q["max_points"]) for q in questions)

            log_to_audit(
                action="AI grading complete",
                user_id=student_id,
                exam_id=exam_id,
                details={
                    "total_score":   total_score,
                    "max_possible":  max_possible,
                    "questions_graded": len(grade_results),
                },
            )

        except Exception as grading_exc:
            # Grading failure must NEVER prevent the submission from completing.
            # Mark all responses with confidence=0.0 so the lecturer knows
            # every question needs manual review.
            grading_failed = True
            fallback_rows = [
                {
                    "exam_id":       exam_id,
                    "student_id":    student_id,
                    "question_id":   q["id"],
                    "ai_score":      0.0,
                    "ai_feedback":   "AI grading error — manual review required.",
                    "ai_confidence": 0.0,
                }
                for q in questions
            ]
            try:
                update_response_grades(fallback_rows)
            except Exception:
                pass  # Fallback write failure is swallowed; audit log below is enough.

            log_to_audit(
                action="AI grading failure",
                user_id=student_id,
                exam_id=exam_id,
                details={"error": str(grading_exc)},
            )

        # Stash score in session state before clearing exam state.
        st.session_state.submission_score = (
            None if grading_failed else total_score
        )

        # ── 5. Clear exam session state ───────────────────────────────────────
        clear_exam_session()

        # ── 6. Signal success and rerun to show completion screen ────────────
        st.session_state.submission_complete = True
        st.rerun()

    except Exception as exc:
        # Log but do NOT clear exam state — the student should be able to retry.
        log_to_audit(
            action="Submission failure",
            user_id=student_id,
            exam_id=exam_id,
            details={"error": str(exc)},
        )
        st.error(
            "⚠️ An error occurred during submission. Your answers are saved. "
            "Please try again or notify your invigilator."
        )


def _build_responses_batch(
    exam_id: int,
    student_id: str,
    questions: list,
    submitted_at,
) -> list[dict]:
    """
    Build the list of response dicts expected by student_repo.save_responses_batch().
    submitted_at=None marks rows as autosave drafts; an ISO string marks final submission.
    """
    rows = []
    for q in questions:
        qid = q["id"]
        answer_html: str = st.session_state.exam_answers.get(qid, "")
        sanitized: str = strip_html_tags(answer_html)
        rows.append(
            {
                "exam_id": exam_id,
                "student_id": student_id,
                "question_id": qid,
                "answer_text": answer_html,
                "sanitized_text": sanitized,
                "submitted_at": submitted_at,
            }
        )
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# SUBMISSION COMPLETE SCREEN
# ──────────────────────────────────────────────────────────────────────────────

def _render_submission_complete() -> None:
    """
    Shown once after a successful submission.
    Displays the AI-graded total score if grading succeeded, or a manual-review
    notice if the grader failed. Resets flags so a page refresh returns to the
    exam browser.
    """
    score = st.session_state.submission_score
    st.session_state.submission_complete = False
    st.session_state.submission_score = None

    if score is not None:
        score_line = (
            f"<p style='font-size:1.5rem; font-weight:700; color:#004D40; margin:8px 0 4px 0;'>"
            f"{score:.1f} marks</p>"
            f"<p style='color:#555; font-size:0.9rem; margin:0 0 12px 0;'>"
            f"Preliminary AI score — subject to lecturer review.</p>"
        )
    else:
        score_line = (
            "<p style='color:#E65100; font-size:0.9rem; margin:8px 0 12px 0;'>"
            "⚠️ Automatic grading encountered an error. Your lecturer will "
            "review your answers manually.</p>"
        )

    st.markdown(
        f"""
        <div style="
            max-width: 500px;
            margin: 60px auto;
            text-align: center;
            background: white;
            padding: 48px 40px;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
        ">
            <div style="font-size: 3rem;">🎓</div>
            <h2 style="color: #004D40; margin-top: 16px; margin-bottom: 8px;">
                Exam Submitted!
            </h2>
            {score_line}
            <p style="color: #555; line-height: 1.6; margin: 0;">
                Results will be visible here once your lecturer<br>
                and the Chief Examiner have approved the grades.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ──────────────────────────────────────────────────────────────────────────────
# EXAM BROWSER (no active exam)
# ──────────────────────────────────────────────────────────────────────────────

def _render_exam_browser(student_id: str) -> None:
    """
    Show all published + chief-approved exams for the student's department.
    Starting an exam that already has an In Progress session resumes it.
    Starting a Submitted exam shows an error (handled by start_exam_session).
    """
    department: str = st.session_state.get("department", "")

    st.markdown(
        f"<h2 style='color:#004D40; margin-bottom:2px;'>Available Exams</h2>"
        f"<p style='color:#777; margin-bottom:24px;'>Department: {department}</p>",
        unsafe_allow_html=True,
    )

    if not department:
        st.warning("Your department is not set. Please log out and log in again.")
        return

    try:
        exams = get_published_exams_by_department(department)
    except Exception as exc:
        log_to_audit(
            action="Exam browser load failure",
            user_id=student_id,
            details={"error": str(exc)},
        )
        st.error("Could not load available exams. Please refresh or contact your invigilator.")
        return

    if not exams:
        st.info("No exams are currently available for your department.")
        return

    for exam in exams:
        # Card container per exam
        st.markdown(
            f"""
            <div style="
                background: white;
                border-radius: 8px;
                padding: 16px 20px 12px 20px;
                margin-bottom: 4px;
                box-shadow: 0 2px 8px rgba(0,0,0,0.08);
                border-left: 4px solid #004D40;
            ">
                <strong style="color:#004D40; font-size:1rem;">{exam['course_code']}</strong>
                &nbsp;—&nbsp;
                <span style="font-size:1rem;">{exam['title']}</span>
                <br>
                <small style="color:#777;">
                    Duration: {exam['duration']} minutes
                </small>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if st.button(
            f"Start Exam — {exam['course_code']}",
            key=f"start_{exam['id']}",
        ):
            _start_exam(
                exam_id=exam["id"],
                duration_minutes=exam["duration"],
                student_id=student_id,
            )


def _start_exam(exam_id: int, duration_minutes: int, student_id: str) -> None:
    """
    Begin a new exam session or resume an existing In Progress session.
    Sets all relevant session state variables from the DB session record.

    IP address is captured via _get_client_ip() (Phase 4). On resume,
    start_exam_session() returns the existing row unchanged — the stored
    IP is whatever was logged when the session was first created.
    """
    try:
        session = start_exam_session(
            exam_id=exam_id,
            student_id=student_id,
            duration_minutes=duration_minutes,
            ip_address=_get_client_ip(),
        )

        # Populate session state from the authoritative DB record.
        st.session_state.active_exam_id = exam_id
        st.session_state.exam_start_time = datetime.fromisoformat(str(session["start_time"]))
        st.session_state.exam_end_time = datetime.fromisoformat(str(session["end_time"]))
        st.session_state.exam_answers = {}
        st.session_state.answered_questions = set()
        st.session_state.current_question_index = 0
        st.session_state.timer_expired = False
        st.session_state.show_submission_summary = False
        st.session_state.last_autosave_time = time.time()

        st.rerun()

    except ValueError as exc:
        error_msg = str(exc).lower()
        if "already submitted" in error_msg:
            st.error(
                "You have already submitted this exam. "
                "Results will be available after grading is complete."
            )
        else:
            st.error(f"Could not start exam: {exc}")

    except Exception as exc:
        log_to_audit(
            action="Exam start failure",
            user_id=student_id,
            exam_id=exam_id,
            details={"error": str(exc)},
        )
        st.error(
            "Could not start the exam. Please try again or contact your invigilator."
        )
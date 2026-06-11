"""
services/result_slip.py — UniGrade Result Slip PDF Generator

Generates a single-page (or multi-page for many questions) official result
slip for a student's exam, returned as raw bytes for Streamlit's
st.download_button.

IMPORTANT — LIBRARY NOTE:
CLAUDE.md §2 lists `pdfplumber` for "result slip generation". pdfplumber is
a PDF *reading* library and cannot create PDFs. This module uses `fpdf2`
instead. Add to requirements.txt:
    fpdf2>=2.7.0
Approval from the architect is required per CLAUDE.md Rule 5. The
functional requirement (PDF result slip export) cannot be met any other way
within the approved stack.

What this module does NOT do:
  - Touch the database.
  - Render any Streamlit UI.
  - Use st.session_state.
"""

from datetime import datetime
from typing import Any

from fpdf import FPDF

# ── Design constants (CLAUDE.md §7.4) ────────────────────────────────────────

_GREEN_R, _GREEN_G, _GREEN_B = 0, 77, 64          # #004D40 Unilorin Green
_GREY_R,  _GREY_G,  _GREY_B  = 245, 247, 248       # #F5F7F8 Background
_TEXT_R,  _TEXT_G,  _TEXT_B  = 33, 33, 33           # Near-black body text
_MUTED_R, _MUTED_G, _MUTED_B = 100, 100, 100        # Muted label text
_RED_R,   _RED_G,   _RED_B   = 198, 40, 40          # #C62828 fail colour
_PAGE_W   = 210                                      # A4 width mm
_MARGIN   = 18                                       # mm
_CONTENT_W = _PAGE_W - 2 * _MARGIN                  # printable width

_PASS_THRESHOLD = 50.0    # percentage — flag for architect if this should vary


# ── Public API ─────────────────────────────────────────────────────────────────

def generate_result_slip(
    student: dict[str, Any],
    exam: dict[str, Any],
    responses: list[dict[str, Any]],
    question_map: dict[int, dict[str, Any]],
) -> bytes:
    """
    Generate a PDF result slip and return it as raw bytes.

    Parameters
    ----------
    student      : Dict with keys: matric_no, name, department, level.
                   All values may be None — the PDF handles missing data
                   gracefully.
    exam         : Dict with keys: course_code, title, session_code,
                   approved_at, id.
    responses    : List of student_responses rows for this exam and student.
                   Expected keys: question_id, ai_score, manual_override,
                   ai_feedback, ai_confidence.
    question_map : {question_id: question dict} with keys: question_number,
                   max_points.

    Returns
    -------
    bytes — PDF file content, ready for st.download_button.
    Never raises — returns a minimal error-state PDF on unexpected failure.
    """
    try:
        pdf = _UniGradePDF()
        pdf.add_page()
        _render_header(pdf, student, exam)
        _render_score_summary(pdf, responses, question_map)
        _render_question_table(pdf, responses, question_map)
        _render_footer(pdf, exam)
        return bytes(pdf.output())

    except Exception as exc:
        # Return a readable error PDF rather than crashing the download button.
        return _error_pdf(str(exc))


# ── PDF subclass ───────────────────────────────────────────────────────────────

class _UniGradePDF(FPDF):
    """
    FPDF subclass with UniGrade brand header and auto page-break footer.
    The header() and footer() methods are called automatically by fpdf2
    on every new page.
    """

    def header(self) -> None:
        # Green banner across the top
        self.set_fill_color(_GREEN_R, _GREEN_G, _GREEN_B)
        self.rect(0, 0, _PAGE_W, 22, style="F")

        self.set_y(5)
        self.set_font("Helvetica", "B", 14)
        self.set_text_color(255, 255, 255)
        self.cell(0, 8, "UNIVERSITY OF ILORIN", align="C", new_x="LMARGIN", new_y="NEXT")

        self.set_font("Helvetica", "", 8)
        self.cell(0, 5, "UniGrade Automated Grading System - Official Result Slip",
                  align="C", new_x="LMARGIN", new_y="NEXT")

        self.set_text_color(_TEXT_R, _TEXT_G, _TEXT_B)
        self.ln(6)

    def footer(self) -> None:
        self.set_y(-14)
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(_MUTED_R, _MUTED_G, _MUTED_B)
        self.cell(
            0, 5,
            "This result slip is computer-generated. Preliminary - subject to "
            "final confirmation by the University Registry.",
            align="C",
            new_x="LMARGIN", new_y="NEXT",
        )
        self.cell(
            0, 4,
            f"Page {self.page_no()} - Printed {datetime.now().strftime('%d %b %Y %H:%M')}",
            align="C",
        )


# ── Section renderers ──────────────────────────────────────────────────────────

def _render_header(
    pdf: _UniGradePDF,
    student: dict[str, Any],
    exam: dict[str, Any],
) -> None:
    """Student identity block + exam details, two-column layout."""

    pdf.set_margins(_MARGIN, pdf.t_margin, _MARGIN)

    # ── Section title ──────────────────────────────────────────────────────────
    _section_title(pdf, "STUDENT INFORMATION")

    col_w = _CONTENT_W / 2

    left_pairs = [
        ("Full Name",       _safe(student.get("name"))),
        ("Matric Number",   _safe(student.get("matric_no"))),
        ("Department",      _safe(student.get("department"))),
        ("Level",           f"{student.get('level', 'N/A')}L"),
    ]
    right_pairs = [
        ("Course Code",     _safe(exam.get("course_code"))),
        ("Course Title",    _safe(exam.get("title"))),
        ("Session Code",    _safe(exam.get("session_code"))),
        ("Approval Date",   _fmt_date(exam.get("approved_at"))),
    ]

    start_y = pdf.get_y()

    # Left column
    for label, value in left_pairs:
        _key_value_row(pdf, label, value, col_w)

    left_end_y = pdf.get_y()

    # Right column — reset Y to align with left start
    pdf.set_xy(_MARGIN + col_w, start_y)
    for label, value in right_pairs:
        _key_value_row(pdf, label, value, col_w, x_offset=_MARGIN + col_w)

    # Advance past whichever column is taller
    pdf.set_y(max(left_end_y, pdf.get_y()) + 4)


def _render_score_summary(
    pdf: _UniGradePDF,
    responses: list[dict[str, Any]],
    question_map: dict[int, dict[str, Any]],
) -> None:
    """Large total-score card with pass/fail indicator."""

    total_earned, total_possible = _compute_totals(responses, question_map)
    percentage = (total_earned / total_possible * 100) if total_possible > 0 else 0.0
    passed = percentage >= _PASS_THRESHOLD

    _section_title(pdf, "RESULT SUMMARY")

    # Shaded summary box
    box_h = 22
    pdf.set_fill_color(_GREY_R, _GREY_G, _GREY_B)
    pdf.rect(_MARGIN, pdf.get_y(), _CONTENT_W, box_h, style="F")

    box_y = pdf.get_y() + 4

    # Score figure
    pdf.set_xy(_MARGIN + 4, box_y)
    pdf.set_font("Helvetica", "B", 22)
    if passed:
        pdf.set_text_color(_GREEN_R, _GREEN_G, _GREEN_B)
    else:
        pdf.set_text_color(_RED_R, _RED_G, _RED_B)

    score_str = f"{total_earned:.1f} / {total_possible:.0f}"
    pdf.cell(70, 10, score_str)

    # Percentage
    pdf.set_font("Helvetica", "", 14)
    pdf.set_text_color(_MUTED_R, _MUTED_G, _MUTED_B)
    pdf.cell(30, 10, f"({percentage:.1f}%)")

    # Pass / Fail badge (right-aligned)
    badge_label = "PASS" if passed else "FAIL"
    pdf.set_xy(_MARGIN + _CONTENT_W - 36, box_y + 1)
    if passed:
        pdf.set_fill_color(_GREEN_R, _GREEN_G, _GREEN_B)
    else:
        pdf.set_fill_color(_RED_R, _RED_G, _RED_B)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(32, 9, badge_label, align="C", fill=True)

    pdf.set_text_color(_TEXT_R, _TEXT_G, _TEXT_B)
    pdf.set_y(pdf.get_y() + box_h + 4)


def _render_question_table(
    pdf: _UniGradePDF,
    responses: list[dict[str, Any]],
    question_map: dict[int, dict[str, Any]],
) -> None:
    """Per-question breakdown table."""

    _section_title(pdf, "QUESTION BREAKDOWN")

    # Column widths (must sum to _CONTENT_W = 174)
    col_q    = 18   # Q No.
    col_score = 28  # Score
    col_max   = 22  # Max
    col_method = 36 # Method
    col_fb    = _CONTENT_W - col_q - col_score - col_max - col_method  # Feedback

    # Table header
    pdf.set_fill_color(_GREEN_R, _GREEN_G, _GREEN_B)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8)
    row_h = 7

    headers = [
        (col_q,     "Q No.",    "C"),
        (col_score, "Score",    "C"),
        (col_max,   "Max",      "C"),
        (col_method,"Method",   "C"),
        (col_fb,    "Feedback", "L"),
    ]
    for w, label, align in headers:
        pdf.cell(w, row_h, label, align=align, fill=True)
    pdf.ln()

    # Sort responses by question number
    sorted_responses = sorted(
        responses,
        key=lambda r: _question_sort_key(
            question_map.get(r["question_id"], {}).get("question_number", "")
        ),
    )

    pdf.set_font("Helvetica", "", 8)
    fill = False

    for r in sorted_responses:
        q = question_map.get(r["question_id"], {})
        qnum    = q.get("question_number", str(r["question_id"]))
        max_pts = float(q.get("max_points", 0))
        score   = _effective_score(r)
        score_s = f"{score:.1f}" if score is not None else "-"
        is_manual = r.get("manual_override") is not None
        method  = "Manual" if is_manual else "AI"
        feedback = _safe(r.get("ai_feedback"))
        # Truncate feedback to keep table readable on the slip
        if len(feedback) > 120:
            feedback = feedback[:117] + "..."

        # Alternate row shading
        if fill:
            pdf.set_fill_color(240, 242, 246)
        else:
            pdf.set_fill_color(255, 255, 255)
        pdf.set_text_color(_TEXT_R, _TEXT_G, _TEXT_B)

        # Use multi_cell for the feedback column; calculate row height first.
        # We render all fixed columns with cell(), then multi_cell for feedback.
        row_start_y = pdf.get_y()
        row_start_x = pdf.get_x()

        pdf.cell(col_q,     row_h, qnum,   align="C", fill=fill)
        pdf.cell(col_score, row_h, score_s, align="C", fill=fill)
        pdf.cell(col_max,   row_h, f"{max_pts:.0f}", align="C", fill=fill)

        # Method badge colouring
        if is_manual:
            pdf.set_text_color(_GREEN_R, _GREEN_G, _GREEN_B)
        else:
            pdf.set_text_color(21, 101, 192)   # blue for AI
        pdf.cell(col_method, row_h, method, align="C", fill=fill)
        pdf.set_text_color(_TEXT_R, _TEXT_G, _TEXT_B)

        # Feedback — multi_cell advances Y; we don't need to ln() after
        fb_x = pdf.get_x()
        pdf.multi_cell(col_fb, row_h, feedback, fill=fill, new_x="LMARGIN", new_y="NEXT")

        fill = not fill

    pdf.ln(3)


def _render_footer(pdf: _UniGradePDF, exam: dict[str, Any]) -> None:
    """Approval metadata and disclaimer block."""

    _section_title(pdf, "APPROVAL")

    approved_at = _fmt_date(exam.get("approved_at"))

    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(_TEXT_R, _TEXT_G, _TEXT_B)
    pdf.multi_cell(
        0, 5,
        f"Grades approved by the Chief Examiner on {approved_at}. "
        "AI-assigned scores have been reviewed by the course lecturer. "
        "Any queries regarding this result should be directed to the "
        "Department Academic Office within 10 working days of publication.",
        new_x="LMARGIN", new_y="NEXT",
    )

    # Signature line
    pdf.ln(8)
    sig_x = _MARGIN
    sig_y = pdf.get_y()
    pdf.set_draw_color(_MUTED_R, _MUTED_G, _MUTED_B)
    pdf.line(sig_x, sig_y, sig_x + 60, sig_y)
    pdf.set_xy(sig_x, sig_y + 1)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(_MUTED_R, _MUTED_G, _MUTED_B)
    pdf.cell(60, 4, "Chief Examiner Signature")


# ── Layout helpers ─────────────────────────────────────────────────────────────

def _section_title(pdf: FPDF, title: str) -> None:
    """Renders a green-underlined section heading."""
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(_GREEN_R, _GREEN_G, _GREEN_B)
    pdf.cell(0, 5, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(_GREEN_R, _GREEN_G, _GREEN_B)
    pdf.line(_MARGIN, pdf.get_y(), _MARGIN + _CONTENT_W, pdf.get_y())
    pdf.ln(3)
    pdf.set_text_color(_TEXT_R, _TEXT_G, _TEXT_B)


def _key_value_row(
    pdf: FPDF,
    label: str,
    value: str,
    col_w: float,
    x_offset: float | None = None,
) -> None:
    """Renders one label: value row within a fixed column width."""
    x = x_offset if x_offset is not None else pdf.get_x()
    pdf.set_x(x)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(_MUTED_R, _MUTED_G, _MUTED_B)
    pdf.cell(col_w * 0.38, 5, label + ":", new_x="RIGHT")

    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(_TEXT_R, _TEXT_G, _TEXT_B)
    pdf.cell(col_w * 0.62, 5, value, new_x="LMARGIN", new_y="NEXT")


# ── Data helpers ───────────────────────────────────────────────────────────────

def _effective_score(r: dict[str, Any]) -> float | None:
    if r.get("manual_override") is not None:
        return float(r["manual_override"])
    if r.get("ai_score") is not None:
        return float(r["ai_score"])
    return None


def _compute_totals(
    responses: list[dict[str, Any]],
    question_map: dict[int, dict[str, Any]],
) -> tuple[float, float]:
    earned = sum(
        _effective_score(r) or 0.0 for r in responses
    )
    possible = sum(
        float(question_map.get(r["question_id"], {}).get("max_points", 0))
        for r in responses
    )
    return earned, possible


def _question_sort_key(qnum: str) -> tuple:
    numeric, alpha = "", ""
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


def _safe(value: object) -> str:
    """Return a printable string; replaces None/empty with an em-dash."""
    s = str(value).strip() if value is not None else ""
    return s if s else "-"


def _fmt_date(value: object) -> str:
    """Format an ISO timestamp or date string to '01 Jan 2024'."""
    if not value:
        return "-"
    raw = str(value)[:10]   # take the date portion only
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%d %b %Y")
    except ValueError:
        return raw


def _error_pdf(error_message: str) -> bytes:
    """Minimal fallback PDF shown when generation fails unexpectedly."""
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(198, 40, 40)
    pdf.cell(0, 10, "Result slip generation failed.", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(33, 33, 33)
    pdf.multi_cell(0, 6, f"Error: {error_message}\n\nPlease contact your system administrator.")
    return bytes(pdf.output())
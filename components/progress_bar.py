"""
components/progress_bar.py — UniGrade Exam Progress Bar

Renders a 10-block visual progress indicator showing how many questions
the student has answered so far.

Display format (CLAUDE.md §7.3):
    [▓▓▓▓▓░░░░░] 5/10 Answered

Colours:
    Completed blocks : #004D40  (Unilorin Green)
    Remaining blocks : #E0E0E0  (Light Grey)

The bar always shows exactly 10 blocks regardless of total question count.
Filled-block count is computed as round(answered / total * 10), clamped to
[0, 10], so the proportions are always visually accurate.
"""

import streamlit as st


# Number of visual blocks — fixed by the design spec.
_TOTAL_BLOCKS = 10

# Unicode block characters used in the spec.
_FILLED = "▓"
_EMPTY  = "░"

# Colour tokens from the design system (CLAUDE.md §7.4 / §7.3).
_COLOR_FILLED    = "#004D40"   # Unilorin Green
_COLOR_EMPTY     = "#E0E0E0"   # Light Grey
_COLOR_LABEL     = "#546E7A"   # Muted text for the count label


def render_progress_bar(total_questions: int) -> None:
    """Render the answer-progress bar for the active exam.

    Reads st.session_state.answered_questions (set of question IDs with a
    non-empty answer). Falls back to an empty set if the key is absent so
    this component is safe to call before the exam session is fully initialised.

    Args:
        total_questions: Total number of questions in the exam. If 0,
                         the component renders nothing (avoids div-by-zero).
    """
    if total_questions <= 0:
        return

    answered_questions: set = st.session_state.get("answered_questions", set())

    # Clamp answered count: can't be negative or exceed total.
    answered_count = max(0, min(len(answered_questions), total_questions))

    # Scale to exactly 10 visual blocks.
    filled_blocks = round(answered_count / total_questions * _TOTAL_BLOCKS)
    filled_blocks = max(0, min(filled_blocks, _TOTAL_BLOCKS))
    empty_blocks  = _TOTAL_BLOCKS - filled_blocks

    # Build coloured HTML spans for each block character.
    filled_html = (
        f'<span style="color:{_COLOR_FILLED};letter-spacing:1px;">'
        f"{_FILLED * filled_blocks}"
        f"</span>"
    )
    empty_html = (
        f'<span style="color:{_COLOR_EMPTY};letter-spacing:1px;">'
        f"{_EMPTY * empty_blocks}"
        f"</span>"
    )
    label_html = (
        f'<span style="color:{_COLOR_LABEL};font-size:0.9rem;margin-left:10px;">'
        f"{answered_count}/{total_questions} Answered"
        f"</span>"
    )

    bar_html = (
        f'<div style="'
        f"font-family:monospace;"
        f"font-size:1.1rem;"
        f"line-height:1.6;"
        f"margin:0.4rem 0 0.8rem 0;"
        f'">'
        f"[{filled_html}{empty_html}]"
        f"{label_html}"
        f"</div>"
    )

    st.markdown(bar_html, unsafe_allow_html=True)
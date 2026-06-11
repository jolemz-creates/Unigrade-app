"""
components/timer.py — UniGrade Exam Countdown Timer

Renders a fixed-position countdown display in the top-right corner of the
exam hall. Fully non-blocking: NO time.sleep() — ever.

How the tick works
------------------
Streamlit rerenders the page on every user interaction (Quill keystrokes,
button clicks) and on explicit st.rerun() calls from the autosave loop in
exam_hall.py. Each rerender calls render_timer(), which reads datetime.now()
and computes the remaining delta from session state. The display therefore
updates on every rerender — it does not run independently.

This means the timer is "accurate" but not "smooth". Accuracy: always correct
to the second at the moment of render. Smoothness: updates whenever the page
rerenders, not on a fixed 1-second interval. This is the correct design for
Streamlit under low-bandwidth conditions (CLAUDE.md §1 — environment constraint).

When time expires, render_timer() sets timer_expired = True and calls
st.rerun() to trigger the auto-submit path in exam_hall.py immediately.
"""

from datetime import datetime

import streamlit as st


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def render_timer() -> None:
    """Render the countdown timer as a fixed top-right overlay.

    Reads st.session_state.exam_end_time (datetime). If None or not set,
    does nothing — safe to call before an exam session is active.

    Side effects on expiry:
      - Sets st.session_state.timer_expired = True
      - Calls st.rerun() to hand control back to exam_hall.py

    CLAUDE.md §7.1 — Never use time.sleep(). Never block.
    """
    end_time = st.session_state.get("exam_end_time")

    if end_time is None:
        return

    remaining_seconds = _compute_remaining(end_time)

    # Update the session state variable (CLAUDE.md §5.2 — time_remaining).
    st.session_state["time_remaining"] = max(0, int(remaining_seconds))

    if remaining_seconds <= 0:
        # Trigger auto-submit path in exam_hall.py.
        st.session_state["timer_expired"] = True
        st.rerun()
        return

    _render_display(remaining_seconds)


def update_time_remaining() -> int:
    """Return current seconds remaining and sync st.session_state.time_remaining.

    Convenience helper for exam_hall.py autosave logic so it can read a
    fresh delta without duplicating the datetime arithmetic.

    Returns 0 if no active exam end time is set.
    """
    end_time = st.session_state.get("exam_end_time")
    if end_time is None:
        return 0

    remaining = max(0, int(_compute_remaining(end_time)))
    st.session_state["time_remaining"] = remaining
    return remaining


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_remaining(end_time: datetime) -> float:
    """Return seconds between now and end_time. Negative when expired."""
    return (end_time - datetime.now()).total_seconds()


def _color_for(remaining_seconds: float) -> str:
    """Return the hex color code matching the urgency level.

    CLAUDE.md §7.1 thresholds:
      < 300 s  ( < 5 min)  → Red    #D32F2F
      < 600 s  (<10 min)   → Orange #F57C00
      otherwise            → Unilorin Green #004D40
    """
    if remaining_seconds < 300:
        return "#D32F2F"
    if remaining_seconds < 600:
        return "#F57C00"
    return "#004D40"


def _format_time(remaining_seconds: float) -> str:
    """Format seconds as HH:MM:SS."""
    total = int(remaining_seconds)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _render_display(remaining_seconds: float) -> None:
    """Inject the fixed-position timer div via st.markdown."""
    color = _color_for(remaining_seconds)
    time_str = _format_time(remaining_seconds)

    # Fixed positioning keeps the timer visible regardless of scroll position.
    # z-index: 9999 ensures it sits above Streamlit's own overlay elements.
    st.markdown(
        f"""
        <div style="
            position: fixed;
            top: 10px;
            right: 20px;
            color: {color};
            font-weight: bold;
            font-size: 1rem;
            font-family: monospace;
            background: rgba(255,255,255,0.92);
            padding: 6px 14px;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.12);
            z-index: 9999;
            letter-spacing: 0.04em;
        ">
            ⏱ Time Remaining: {time_str}
        </div>
        """,
        unsafe_allow_html=True,
    )
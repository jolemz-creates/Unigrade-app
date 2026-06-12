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

import time
from datetime import datetime

import streamlit as st

from services.audit import log_to_audit

# ── Focus-loss detection (Phase 4 — Option C) ─────────────────────────────────
# If the gap between consecutive timer renders exceeds this threshold, we treat
# it as a probable focus-loss event (tab hidden, laptop closed, alt-tabbed).
#
# Threshold = autosave_interval (30 s) + 15 s grace for slow networks / UI lag.
# Set to 0 in tests by patching FOCUS_LOSS_THRESHOLD_SECONDS.
FOCUS_LOSS_THRESHOLD_SECONDS: int = 45


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

    Focus-loss detection (Phase 4 — Option C):
      On each render, computes the wall-clock gap since the previous render.
      A gap > FOCUS_LOSS_THRESHOLD_SECONDS during an active exam is logged to
      audit_log as a probable focus-loss event. This detects tab switching,
      laptop lid closes, and browser minimisation — all of which suppress
      Streamlit reruns, creating a detectable silence in the render stream.

      False-positive risk: a student who stops interacting entirely for >45 s
      (e.g. thinking hard without typing) will also trigger this. The log entry
      is advisory ("Possible focus loss") — it is not an automatic disqualifier.

    CLAUDE.md §7.1 — Never use time.sleep(). Never block.
    """
    end_time = st.session_state.get("exam_end_time")

    if end_time is None:
        return

    # ── Focus-loss detection ──────────────────────────────────────────────────
    _check_focus_loss()

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
# Focus-loss detection (Phase 4 — Option C)
# ---------------------------------------------------------------------------

def _check_focus_loss() -> None:
    """
    Detect probable focus-loss events by measuring the gap between renders.

    Stores the wall-clock timestamp of each render in session state under
    'last_timer_render'. On the next render, the gap is computed. If it
    exceeds FOCUS_LOSS_THRESHOLD_SECONDS and the exam is active, a record
    is written to audit_log.

    Called only from render_timer(), which already guards on exam_end_time
    being set — so this function only runs during an active exam session.

    Rules:
    - First render (last_timer_render == 0.0): stamp and return. No gap yet.
    - Subsequent renders: compute gap, log if above threshold, then re-stamp.
    - Never raises — swallows all exceptions so a logging failure cannot
      interrupt the timer render or the exam session.
    """
    now: float = time.time()

    # Initialise on first call (setdefault respects existing values).
    st.session_state.setdefault("last_timer_render", 0.0)
    last: float = st.session_state["last_timer_render"]

    try:
        if last != 0.0:
            gap: float = now - last
            if gap > FOCUS_LOSS_THRESHOLD_SECONDS:
                user_id = st.session_state.get("user_id")
                exam_id = st.session_state.get("active_exam_id")
                log_to_audit(
                    action="Possible focus loss detected",
                    user_id=user_id,
                    exam_id=exam_id,
                    details={
                        "gap_seconds": round(gap, 1),
                        "threshold_seconds": FOCUS_LOSS_THRESHOLD_SECONDS,
                        "detection_method": "timer_render_gap",
                    },
                )
    except Exception:
        # Never let a logging failure surface to the student UI.
        pass
    finally:
        # Always update the stamp, even if the log call failed.
        st.session_state["last_timer_render"] = now


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
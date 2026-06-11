"""
pages/chief_examiner/audit_log.py
───────────────────────────────────
Read-only audit trail viewer for the Chief Examiner.

Displays all entries from audit_log, optionally pre-filtered to a single exam
(when arriving from approval.py via st.session_state.audit_view_exam_id).
No edit controls exist on this page — the audit log is append-only by design.

Columns rendered: Timestamp | Action | User | Details (JSON pretty-printed)
"""

import json
import streamlit as st

from models.audit_repo import get_audit_logs


# ── Constants ─────────────────────────────────────────────────────────────────

_DEFAULT_LIMIT = 200
_DETAILS_TRUNCATE = 400   # chars before collapsing raw JSON in the table cell


# ── Main Entry Point ──────────────────────────────────────────────────────────

def render_audit_log() -> None:
    """Called by app.py after Chief Examiner routing."""

    # ── Page Guard ─────────────────────────────────────────────────────────────
    if not st.session_state.get("logged_in"):
        st.error("Unauthorized. Please log in.")
        st.stop()

    if st.session_state.get("role") != "Chief Examiner":
        st.error("Access denied. Chief Examiner only.")
        st.stop()

    # ── Session state defaults ─────────────────────────────────────────────────
    st.session_state.setdefault("audit_view_exam_id", None)

    # ── Page Header ────────────────────────────────────────────────────────────
    exam_id: int | None = st.session_state.audit_view_exam_id

    if exam_id:
        subtitle = f"Filtered to Exam ID <strong>{exam_id}</strong>"
    else:
        subtitle = "Showing all entries"

    st.markdown(
        "<h2 style='color:#004D40;margin-bottom:4px;'>Audit Log</h2>"
        f"<p style='color:#555;margin-top:0;'>{subtitle}</p>",
        unsafe_allow_html=True,
    )

    # ── Filter Controls ────────────────────────────────────────────────────────
    col_filter, col_clear = st.columns([3, 1])

    with col_filter:
        # Allow the Chief Examiner to manually enter an exam ID filter,
        # or leave blank to show all. Initialise from session state.
        typed_id = st.number_input(
            "Filter by Exam ID (leave 0 to show all)",
            min_value=0,
            value=int(exam_id) if exam_id else 0,
            step=1,
            key="audit_exam_id_input",
        )
        active_filter: int | None = int(typed_id) if typed_id > 0 else None

    with col_clear:
        st.markdown("&nbsp;", unsafe_allow_html=True)   # vertical alignment shim
        if st.button("Clear Filter", key="audit_clear_filter"):
            st.session_state.audit_view_exam_id = None
            st.rerun()

    # Sync typed value back to session state so approval.py round-trips work.
    st.session_state.audit_view_exam_id = active_filter

    st.divider()

    # ── Load Logs ──────────────────────────────────────────────────────────────
    logs: list[dict] = get_audit_logs(exam_id=active_filter, limit=_DEFAULT_LIMIT)

    if not logs:
        if active_filter:
            st.info(f"No audit entries found for Exam ID {active_filter}.")
        else:
            st.info("The audit log is empty.")
        return

    # ── Result Count ──────────────────────────────────────────────────────────
    shown = len(logs)
    st.caption(
        f"Showing {shown} {'entry' if shown == 1 else 'entries'}"
        + (f" (limit {_DEFAULT_LIMIT})" if shown == _DEFAULT_LIMIT else "")
        + (" — most recent first" if shown > 1 else "")
    )

    # ── Table ──────────────────────────────────────────────────────────────────
    # Rendered as manual rows (not st.dataframe) so the details column can
    # display pretty-printed JSON without being truncated by Streamlit's
    # internal cell renderer.

    # Header row
    hcol_ts, hcol_action, hcol_user, hcol_details = st.columns([2, 3, 2, 4])
    hcol_ts.markdown("**Timestamp**")
    hcol_action.markdown("**Action**")
    hcol_user.markdown("**User**")
    hcol_details.markdown("**Details**")

    st.markdown(
        "<hr style='border:1px solid #E0E0E0;margin:4px 0 8px 0;'>",
        unsafe_allow_html=True,
    )

    for entry in logs:
        _render_log_row(entry)


# ── Row Renderer ───────────────────────────────────────────────────────────────

def _render_log_row(entry: dict) -> None:
    """Renders a single audit_log row with pretty-printed details."""

    timestamp: str = entry.get("timestamp") or "—"
    action: str = entry.get("action") or "—"
    user_id = entry.get("user_id")
    user_label: str = str(user_id) if user_id is not None else "system"
    raw_details = entry.get("details")

    pretty_details = _format_details(raw_details)

    col_ts, col_action, col_user, col_details = st.columns([2, 3, 2, 4])

    with col_ts:
        # Trim microseconds if present: "2024-05-01 14:32:11.000000" → "2024-05-01 14:32:11"
        st.markdown(
            f"<span style='font-size:12px;color:#555;'>{timestamp[:19]}</span>",
            unsafe_allow_html=True,
        )

    with col_action:
        st.markdown(f"<span style='font-size:13px;'>{action}</span>", unsafe_allow_html=True)

    with col_user:
        st.markdown(
            f"<code style='font-size:12px;'>{user_label}</code>",
            unsafe_allow_html=True,
        )

    with col_details:
        if pretty_details:
            # Long JSON goes into an expander to keep the table scannable.
            if len(pretty_details) > _DETAILS_TRUNCATE:
                with st.expander("View details"):
                    st.code(pretty_details, language="json")
            else:
                st.code(pretty_details, language="json")
        else:
            st.markdown(
                "<span style='color:#aaa;font-size:12px;'>—</span>",
                unsafe_allow_html=True,
            )

    st.markdown(
        "<hr style='border:0;border-top:1px solid #F0F0F0;margin:4px 0;'>",
        unsafe_allow_html=True,
    )


# ── Details Formatter ──────────────────────────────────────────────────────────

def _format_details(raw: object) -> str:
    """
    Converts the details field to a pretty-printed JSON string.

    Handles three cases:
      - None / empty string → returns ""
      - Valid JSON string   → parses and re-serialises with indent=2
      - Malformed string    → returns the raw string as-is so nothing is lost
    """
    if not raw:
        return ""

    if isinstance(raw, dict):
        # Already deserialised (shouldn't happen with raw sqlite3, but safe).
        return json.dumps(raw, indent=2, ensure_ascii=False)

    raw_str = str(raw).strip()
    if not raw_str:
        return ""

    try:
        parsed = json.loads(raw_str)
        return json.dumps(parsed, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, ValueError):
        # Return verbatim — don't lose data because of a formatting failure.
        return raw_str
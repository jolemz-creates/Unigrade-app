"""
app.py — UniGrade Main Router

Entry point for all Streamlit traffic. Reads the `view` query parameter and
dispatches to the correct portal. Staff portals are intentionally hidden from
the default student UI (CLAUDE.md §3.2 — never violate).

Routing table:
  /?view=student  (default)  → Student login / Student portal
  /?view=staff               → Staff login / Lecturer or Chief Examiner dashboard
  /?view=<anything else>     → Error + stop
"""

import streamlit as st

# set_page_config MUST be the first Streamlit call in the script.
st.set_page_config(
    page_title="UniGrade — Unilorin Grading System",
    page_icon="🎓",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# ---------------------------------------------------------------------------
# Internal imports — after set_page_config
# ---------------------------------------------------------------------------
from auth.auth import (
    init_session_defaults,
    login_lecturer,
    login_student,
    clear_exam_session,
)
from models.db_manager import init_db

# Page imports are guarded so app.py is runnable before Phase 2B files exist.
try:
    from pages.student.exam_hall import render_exam_hall
    _EXAM_HALL_AVAILABLE = True
except ImportError:
    _EXAM_HALL_AVAILABLE = False

try:
    from pages.student.results import render_results
    _RESULTS_AVAILABLE = True
except ImportError:
    _RESULTS_AVAILABLE = False

try:
    from pages.lecturer.dashboard import render_dashboard
    _DASHBOARD_AVAILABLE = True
except ImportError:
    _DASHBOARD_AVAILABLE = False

try:
    from pages.lecturer.grading_review import render_grading_review
    _GRADING_REVIEW_AVAILABLE = True
except ImportError:
    _GRADING_REVIEW_AVAILABLE = False

try:
    from pages.chief_examiner.approval import render_approval
    _APPROVAL_AVAILABLE = True
except ImportError:
    _APPROVAL_AVAILABLE = False

try:
    from pages.chief_examiner.audit_log import render_audit_log
    _AUDIT_LOG_AVAILABLE = True
except ImportError:
    _AUDIT_LOG_AVAILABLE = False


# ---------------------------------------------------------------------------
# Global Styles (Design System — CLAUDE.md §7.4)
# ---------------------------------------------------------------------------

def _inject_global_css() -> None:
    st.markdown(
        """
        <style>
        /* Global background */
        .stApp {
            background-color: #F5F7F8;
        }

        /* Hide default Streamlit hamburger menu and footer */
        #MainMenu { visibility: hidden; }
        footer    { visibility: hidden; }

        /* Centered card container for login forms */
        .unigrade-card {
            background: #FFFFFF;
            border-radius: 12px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.08);
            padding: 2rem 2.5rem;
            max-width: 500px;
            margin: 2rem auto;
        }

        /* Primary button overrides */
        .stButton > button {
            background-color: #004D40;
            color: #FFFFFF;
            border: none;
            border-radius: 8px;
            padding: 0.5rem 1.25rem;
            font-weight: 600;
            width: 100%;
            transition: background-color 0.2s ease;
        }
        .stButton > button:hover {
            background-color: #00695C;
            color: #FFFFFF;
        }

        /* Input fields — light grey, no visible border */
        .stTextInput > div > div > input,
        .stSelectbox > div > div > div,
        .stNumberInput > div > div > input {
            background-color: #F0F2F6 !important;
            border: none !important;
            border-radius: 6px !important;
        }

        /* Unilorin Green headers */
        h1, h2 { color: #004D40; }

        /* Dashboard max-width */
        .block-container {
            max-width: 800px;
            padding-top: 1.5rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


# ---------------------------------------------------------------------------
# Student Login & Registration
# ---------------------------------------------------------------------------

def render_student_login() -> None:
    """Student authentication via matric number.

    First-time students (not in the DB) are shown an inline registration
    form to capture name, department, and level before logging in.
    """
    from models.student_repo import create_student

    st.markdown(
        '<div style="text-align:center;margin-bottom:1.5rem;">'
        '<h1 style="color:#004D40;">🎓 UniGrade</h1>'
        '<p style="color:#546E7A;">University of Ilorin — Automated Grading System</p>'
        '</div>',
        unsafe_allow_html=True,
    )

    with st.container():
        st.subheader("Student Portal")

        matric_no = st.text_input(
            "Matric Number",
            placeholder="e.g. 21/52CS001",
            key="login_matric_input",
        ).strip()

        # If we already know this is a new student, show registration fields.
        is_new_student = st.session_state.get("_new_student_matric") == matric_no and matric_no

        if is_new_student:
            st.info("Matric number not found. Complete your profile to continue.")
            reg_name = st.text_input("Full Name", key="reg_name").strip()
            reg_dept = st.text_input("Department", placeholder="e.g. Computer Science", key="reg_dept").strip()
            reg_level = st.selectbox(
                "Level", options=[100, 200, 300, 400, 500], key="reg_level"
            )
            reg_email = st.text_input(
                "Email (optional)", placeholder="e.g. student@unilorin.edu.ng", key="reg_email"
            ).strip()

            if st.button("Register & Continue", key="btn_register"):
                if not reg_name or not reg_dept:
                    st.error("Name and department are required.")
                else:
                    create_student(
                        matric_no=matric_no,
                        name=reg_name,
                        department=reg_dept,
                        level=reg_level,
                        email=reg_email or None,
                    )
                    st.session_state.pop("_new_student_matric", None)
                    _complete_student_login(matric_no)
        else:
            if st.button("Sign In", key="btn_student_login"):
                if not matric_no:
                    st.error("Please enter your matric number.")
                    return

                student = login_student(matric_no)
                if student is None:
                    # Flag as new student; rerun will show registration form.
                    st.session_state["_new_student_matric"] = matric_no
                    st.rerun()
                else:
                    _complete_student_login(matric_no)


def _complete_student_login(matric_no: str) -> None:
    """Populate session state after successful student authentication."""
    from models.student_repo import get_student_by_matric

    student = get_student_by_matric(matric_no)
    if student is None:
        st.error("Login failed. Please try again.")
        return

    st.session_state["logged_in"] = True
    st.session_state["role"] = "Student"
    st.session_state["user_id"] = student["matric_no"]
    st.session_state["user_name"] = student.get("name") or matric_no
    st.session_state["department"] = student.get("department", "")
    st.rerun()


# ---------------------------------------------------------------------------
# Staff Login
# ---------------------------------------------------------------------------

def render_staff_login() -> None:
    """Lecturer / Chief Examiner authentication via staff ID + password."""
    st.markdown(
        '<div style="text-align:center;margin-bottom:1.5rem;">'
        '<h1 style="color:#004D40;">🎓 UniGrade</h1>'
        '<p style="color:#546E7A;">Staff Portal — University of Ilorin</p>'
        '</div>',
        unsafe_allow_html=True,
    )

    with st.container():
        st.subheader("Staff Login")

        staff_id = st.text_input(
            "Staff ID",
            placeholder="e.g. UNILORIN/LEC/0042",
            key="login_staff_id",
        ).strip()

        password = st.text_input(
            "Password",
            type="password",
            key="login_staff_password",
        )

        if st.button("Sign In", key="btn_staff_login"):
            if not staff_id or not password:
                st.error("Staff ID and password are required.")
                return

            lecturer = login_lecturer(staff_id, password)
            if lecturer is None:
                st.error("Invalid staff ID or password.")
                return

            st.session_state["logged_in"] = True
            st.session_state["role"] = lecturer["role"]
            st.session_state["user_id"] = lecturer["id"]
            st.session_state["user_name"] = lecturer["name"]
            st.session_state["department"] = lecturer.get("department", "")
            st.rerun()


# ---------------------------------------------------------------------------
# Student Portal Dispatcher
# ---------------------------------------------------------------------------

def render_student_portal() -> None:
    """Render the appropriate student page based on current state.

    Navigation logic:
    - Active exam in session → exam hall
    - No active exam → exam list (exam hall handles this)
    - Results flag set → results page
    """
    _render_student_nav()

    # Results view requested explicitly
    if st.session_state.get("_view_results"):
        if _RESULTS_AVAILABLE:
            render_results()
        else:
            _placeholder("Student Results", "pages/student/results.py")
        return

    # Default: exam hall (handles both exam list and active exam)
    if _EXAM_HALL_AVAILABLE:
        render_exam_hall()
    else:
        _placeholder("Student Exam Hall", "pages/student/exam_hall.py")


def _render_student_nav() -> None:
    """Minimal top-bar for the student portal."""
    st.markdown(
        "<style>div.block-container{padding-top:3rem;}</style>",
        unsafe_allow_html=True,
    )

    col_name, col_results, col_logout = st.columns([3, 1, 1])

    with col_name:
        st.markdown(
            f"**👤 {st.session_state.user_name}** · {st.session_state.department}",
            unsafe_allow_html=False,
        )

    with col_results:
        if st.button("My Results", key="nav_results"):
            st.session_state["_view_results"] = True
            st.rerun()

    with col_logout:
        if st.button("Log Out", key="nav_student_logout"):
            _logout()

    st.divider()


# ---------------------------------------------------------------------------
# Staff Dashboard Dispatcher
# ---------------------------------------------------------------------------

def render_staff_dashboard() -> None:
    """Route to the correct staff page based on role and sub-navigation."""
    role = st.session_state.role
    _render_staff_nav(role)

    sub_view = st.session_state.get("_staff_sub_view", "dashboard")

    if role == "Chief Examiner":
        _render_chief_examiner_view(sub_view)
    else:
        _render_lecturer_view(sub_view)


def _render_lecturer_view(sub_view: str) -> None:
    if sub_view == "grading_review":
        if _GRADING_REVIEW_AVAILABLE:
            render_grading_review()
        else:
            _placeholder("Grading Review", "pages/lecturer/grading_review.py")
    else:
        # Default: exam dashboard
        if _DASHBOARD_AVAILABLE:
            render_dashboard()
        else:
            _placeholder("Lecturer Dashboard", "pages/lecturer/dashboard.py")


def _render_chief_examiner_view(sub_view: str) -> None:
    if sub_view == "audit":
        if _AUDIT_LOG_AVAILABLE:
            render_audit_log()
        else:
            _placeholder("Audit Log", "pages/chief_examiner/audit_log.py")
    elif sub_view == "grading_review":
        if _GRADING_REVIEW_AVAILABLE:
            render_grading_review()
        else:
            _placeholder("Grading Review", "pages/lecturer/grading_review.py")
    else:
        # Default: approval queue
        if _APPROVAL_AVAILABLE:
            render_approval()
        else:
            _placeholder("Grade Approval", "pages/chief_examiner/approval.py")


def _render_staff_nav(role: str) -> None:
    """Top navigation bar for all staff roles."""
    nav_cols = st.columns([3, 1, 1, 1])

    with nav_cols[0]:
        st.markdown(
            f"**🎓 UniGrade** · {st.session_state.user_name} "
            f"<span style='color:#004D40;font-size:0.85em;'>({role})</span>",
            unsafe_allow_html=True,
        )

    with nav_cols[1]:
        label = "Approvals" if role == "Chief Examiner" else "My Exams"
        if st.button(label, key="nav_dashboard"):
            st.session_state["_staff_sub_view"] = "dashboard"
            st.rerun()

    with nav_cols[2]:
        extra_label = "Audit Log" if role == "Chief Examiner" else "Grade Review"
        extra_view = "audit" if role == "Chief Examiner" else "grading_review"
        if st.button(extra_label, key="nav_extra"):
            st.session_state["_staff_sub_view"] = extra_view
            st.rerun()

    with nav_cols[3]:
        if st.button("Log Out", key="nav_staff_logout"):
            _logout()

    st.divider()


# ---------------------------------------------------------------------------
# Shared Utilities
# ---------------------------------------------------------------------------

def _logout() -> None:
    """Clear all session state and redirect to the appropriate login page."""
    view = st.query_params.get("view", "student")
    # Preserve only the view param; nuke everything else.
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    st.query_params["view"] = view
    st.rerun()


def _placeholder(page_name: str, module_path: str) -> None:
    """Shown when a page module has not been built yet (pre-Phase 2B)."""
    st.info(
        f"**{page_name}** is not yet available.  \n"
        f"Build `{module_path}` in Phase 2B to activate this view.",
        icon="🚧",
    )


# ---------------------------------------------------------------------------
# Main Router — CLAUDE.md §3.2
# ---------------------------------------------------------------------------

def main() -> None:
    # Ensure DB schema is present (idempotent).
    init_db()

    # Inject design system CSS.
    _inject_global_css()

    # Initialize session state defaults before any render path.
    init_session_defaults()

    # Read the `view` param; default to "student".
    view = st.query_params.get("view", "student")

    if view == "student":
        if st.session_state.logged_in and st.session_state.role == "Student":
            render_student_portal()
        else:
            # Clear any stale auth state before showing login.
            if st.session_state.logged_in:
                _logout()
            render_student_login()

    elif view == "staff":
        if st.session_state.logged_in and st.session_state.role in (
            "Lecturer", "Chief Examiner"
        ):
            render_staff_dashboard()
        else:
            if st.session_state.logged_in:
                _logout()
            render_staff_login()

    else:
        # CLAUDE.md §3.2 — invalid routes hard-stop.
        st.error("Invalid route.")
        st.stop()


if __name__ == "__main__":
    main()
# UniGrade — Unilorin Automated Grading System

An AI-powered semantic grading platform built for the University of Ilorin (Unilorin). UniGrade replaces slow, manually-graded short-answer exams with a system that evaluates student responses for _conceptual understanding_ — not rigid keyword matching — using a large language model, while keeping lecturers and the Chief Examiner firmly in control of final results.

## Description

Grading hundreds of theoretical short-answer scripts by hand is slow, exhausting, and prone to inconsistency between graders. UniGrade addresses this by having students submit answers through a clean, distraction-free exam interface. On submission, every response is sent in a single batch to an LLM (Groq's Llama 3.3 70B), which scores each answer on semantic equivalence to a lecturer-defined model answer and rubric, returning a score, written feedback, and a confidence rating.

Low-confidence, borderline, or statistically anomalous scores are automatically flagged for human review. Lecturers can override any AI score with a mandatory justification, and every override is permanently logged. No result reaches a student until the Chief Examiner has reviewed and explicitly published it.

The system is built to run reliably under exam-day conditions: hundreds of students submitting within the same few minutes, on low-bandwidth university networks, without locking up the database or losing a single typed answer.

## Tech Stack

- **Language:** Python 3.10+
- **Frontend / Routing:** Streamlit
- **Rich Text Editor:** `streamlit-quill`
- **Database:** SQLite3 (WAL mode, raw `sqlite3` library)
- **AI Engine:** Groq API — Llama 3.3 70B
- **HTML Sanitization:** `bleach` + `beautifulsoup4`
- **Password Hashing:** `bcrypt` (work factor 12)
- **PDF Result Slips:** `fpdf2`
- **Environment Management:** `python-dotenv`
- **Async Grading:** `asyncio` + `aiohttp` (`asyncio.Semaphore(30)`)

## Prerequisites

Before installing, make sure you have:

- **Python 3.10 or higher** — [python.org/downloads](https://www.python.org/downloads/)
- **Git** — to clone the repository
- **A Groq API key** — free to generate at [console.groq.com](https://console.groq.com)
- (Recommended) **A virtual environment tool** — `venv` ships with Python by default

## Installation

```bash
# 1. Clone the repository
git clone https://github.com/jolemz-creates/<repo-name>.git
cd Unigrade

# 2. Create and activate a virtual environment
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS / Linux
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
# Create a .env file in the project root and add your Groq API key:
echo GROQ_API_KEY=your_key_here > .env
```

The database (`database/unigrade.db`) is created automatically with all required tables, indexes, and WAL mode enabled the first time the app runs — no manual setup needed.

## How to Run

```bash
streamlit run app.py
```

The app opens in your browser, defaulting to the **Student Portal**.

**Staff access (Lecturer / Chief Examiner)** is intentionally hidden from the default UI. Access it via:

```
http://localhost:8501/?view=staff
```

| Role           | Access                                           |
| -------------- | ------------------------------------------------ |
| Student        | Default URL — matric number login                |
| Lecturer       | `?view=staff` — Staff ID + password              |
| Chief Examiner | `?view=staff` — Staff ID + password (role-based) |

## Project Structure

```
unigrade/
├── app.py                      # Main entry point & URL router (?view=staff)
├── .env                         # GROQ_API_KEY (not committed)
├── requirements.txt
│
├── database/
│   ├── schema.sql                # Table definitions, indexes, constraints
│   └── unigrade.db                # Runtime SQLite database (not committed)
│
├── auth/
│   └── auth.py                    # bcrypt login, session state, role routing
│
├── services/                    # Business logic — no UI, no direct DB access
│   ├── grader.py                  # Groq integration, prompt builder, validation
│   ├── sanitizer.py               # HTML stripping before AI calls
│   └── audit.py                   # log_to_audit() helper
│
├── models/                       # All database interaction
│   ├── db_manager.py              # Connection factory, init_db(), PRAGMAs
│   ├── lecturer_repo.py
│   ├── exam_repo.py
│   ├── student_repo.py
│   └── audit_repo.py
│
├── components/                  # Reusable Streamlit UI pieces
│   ├── timer.py                   # Non-blocking countdown widget
│   └── progress_bar.py            # [▓▓▓░░] 3/10 Answered widget
│
├── pages/
│   ├── student/
│   │   ├── exam_hall.py            # Timer, Quill editor, autosave, progress bar
│   │   └── results.py              # Published results view
│   ├── lecturer/
│   │   ├── dashboard.py             # Exam CRUD
│   │   ├── question_editor.py       # Hierarchical question builder (Q1 → Q1a)
│   │   └── grading_review.py        # Flagged scores, manual override
│   └── chief_examiner/
│       ├── approval.py              # Approve/reject, publish results
│       └── audit_log.py             # Full audit trail viewer
│
└── tests/
    ├── test_database.py            # Schema, WAL, foreign keys, cascades
    ├── test_sanitizer.py           # HTML stripping edge cases
    ├── test_grader.py              # Mock Groq responses, score validation
    ├── test_auth.py                # Login, bcrypt, session state
    └── test_concurrency.py         # WAL stress test under concurrent writes
```

## User Roles at a Glance

| Role               | Capabilities                                                                          |
| ------------------ | ------------------------------------------------------------------------------------- |
| **Student**        | Take exams, view own results (post-publication)                                       |
| **Lecturer**       | Create exams, author questions/rubrics, review AI grades, override scores with reason |
| **Chief Examiner** | All Lecturer permissions + approve/reject grades, audit overrides, publish results    |

## Security Notes

- Lecturer and Chief Examiner accounts use `bcrypt`-hashed passwords (work factor 12) — never plaintext.
- All AI calls strip rich-text HTML to plain text before transmission; raw HTML and sanitized text are both stored for audit purposes.
- Once an exam session is marked `Submitted`, student answers are immutable — lecturers may only adjust the `manual_override` field, with every change logged to `audit_log`.
- SQLite runs in WAL mode with foreign keys enforced on every connection, supporting concurrent writes during high-traffic exam submissions.

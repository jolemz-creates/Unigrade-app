"""
models/db_manager.py — UniGrade Database Connection Factory

Rules (from CLAUDE.md §4.1):
  - PRAGMA journal_mode=WAL  must be set on every connection.
  - PRAGMA foreign_keys=ON   must be set on every connection.
  - NEVER open a connection outside a `with` block.
  - NO global connection variables.

Usage pattern for all callers:

    from models.db_manager import get_connection

    with get_connection() as conn:
        rows = conn.execute("SELECT ...").fetchall()

get_connection() is a context manager that:
  __enter__  → opens the connection, sets PRAGMAs, returns conn
  __exit__   → commits on success, rolls back on exception, then CLOSES
               the connection. Closing on exit is critical on Windows,
               where an open SQLite handle is an exclusive file lock that
               blocks os.unlink() (e.g. in test tearDown cleanup).
"""

import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

# Absolute path to unigrade.db, resolved relative to this file.
# Public (no leading underscore) so test setUp can patch it via:
#   unittest.mock.patch("models.db_manager.DB_PATH", new=tmp_path)
DB_PATH = Path(__file__).parent.parent / "database" / "unigrade.db"

# Absolute path to the SQL schema file.
SCHEMA_PATH = Path(__file__).parent.parent / "database" / "schema.sql"


@contextmanager
def get_connection() -> Iterator[sqlite3.Connection]:
    """
    Open, configure, and automatically close a SQLite connection.

    Yields the connection inside a transaction so callers use it as:

        with get_connection() as conn:
            conn.execute(...)

    PRAGMAs set on every connection:
      - journal_mode=WAL     : Concurrent read/write without exclusive locks.
      - foreign_keys=ON      : Enforce referential integrity on every write.
      - busy_timeout=5000    : Wait up to 5 seconds when a write lock is held
                               by another thread before raising OperationalError.
                               Required for Phase 4 WAL stress test correctness —
                               WAL mode alone does not queue concurrent writers;
                               busy_timeout is what prevents "database is locked"
                               errors under peak load (500 concurrent submissions).

    Behaviour:
      - Commits on clean exit from the `with` block.
      - Rolls back and re-raises on any exception inside the block.
      - Always closes the connection in the `finally` clause, releasing
        the file handle on Windows and preventing tearDown PermissionErrors
        in tests.

    Raises:
        sqlite3.OperationalError: if the database file cannot be opened.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row  # Rows accessible by column name
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=5000;")  # Phase 4: wait up to 5 s on write contention
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()  # Always releases the file handle — critical on Windows

def _migrate_db(conn: sqlite3.Connection) -> None:
    """Apply incremental schema migrations to existing databases.
    Safe to run on every startup — all migrations are idempotent.
    """
    # Migration: add password_hash to students if not present
    existing_cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(students)").fetchall()
    }
    if "password_hash" not in existing_cols:
        conn.execute("ALTER TABLE students ADD COLUMN password_hash TEXT")

def init_db() -> None:
    """
    Initialise the database by executing database/schema.sql.

    Safe to call on every application startup — all DDL statements use
    CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS, so repeated
    calls are idempotent and will not destroy existing data.

    Raises:
        FileNotFoundError: if schema.sql cannot be found at the expected path.
        sqlite3.DatabaseError: if any SQL statement in the schema is invalid.
    """
    if not SCHEMA_PATH.exists():
        raise FileNotFoundError(
            f"Schema file not found at {SCHEMA_PATH}. "
            "Ensure database/schema.sql is present in the project root."
        )

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    with get_connection() as conn:
        # executescript() issues an implicit COMMIT before running, which is
        # required for DDL in SQLite. It also runs outside the normal
        # transaction managed by the context manager, which is fine here
        # because schema init is an administrative, not a data, operation.
        conn.executescript(schema_sql)
        _migrate_db(conn)   
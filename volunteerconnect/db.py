"""Database helpers for PostgreSQL on Neon.

All SQL uses %s placeholders with a separate params tuple. Never build SQL
by putting values into the string yourself.
"""
import logging
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from flask import current_app
from psycopg.rows import dict_row
from werkzeug.security import generate_password_hash

log = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

# Any fixed number works. Every startup takes this lock, so if several
# gunicorn workers or Cloud Run instances start at once, they run the
# schema and seed one at a time instead of racing each other.
STARTUP_LOCK_ID = 727001

# Werkzeug's default (scrypt) is missing from macOS's built-in Python, so we
# name a method that works everywhere. Use this same constant when checking
# passwords is added in the accounts feature.
PASSWORD_HASH_METHOD = "pbkdf2:sha256"


def get_conn(database_url=None):
    """Open a new connection. Rows come back as dicts, e.g. row["email"]."""
    url = database_url or current_app.config["DATABASE_URL"]
    return psycopg.connect(url, row_factory=dict_row)


@contextmanager
def transaction(database_url=None):
    """Run several statements as one all-or-nothing unit.

    Usage:
        with transaction() as cur:
            cur.execute("SELECT ... FOR UPDATE", (shift_id,))
            cur.execute("INSERT ...", (...))

    If the block finishes, everything is committed. If it raises, everything
    is rolled back.
    """
    with get_conn(database_url) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                yield cur


def query(sql, params=(), one=False):
    """Run a single statement in its own transaction.

    Returns a list of rows, or one row (or None) when one=True. Statements
    without results (e.g. an UPDATE without RETURNING) return [] or None.
    """
    with transaction() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() if cur.description else []
    if one:
        return rows[0] if rows else None
    return rows


def init_db(database_url):
    """Create any missing tables, then seed the coordinator. Safe to run on every start."""
    schema_sql = SCHEMA_PATH.read_text()
    with transaction(database_url) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (STARTUP_LOCK_ID,))
        cur.execute(schema_sql)
        seed_coordinator(cur)


def seed_coordinator(cur):
    """Create the coordinator account from env vars, only if none exists yet."""
    cur.execute("SELECT 1 FROM users WHERE role = 'coordinator' LIMIT 1")
    if cur.fetchone():
        return

    email = os.environ.get("COORDINATOR_EMAIL")
    password = os.environ.get("COORDINATOR_PASSWORD")
    if not email or not password:
        log.warning("No coordinator exists and COORDINATOR_EMAIL/PASSWORD are not set; skipping seed.")
        return

    cur.execute(
        "INSERT INTO users (email, password_hash, role, status) "
        "VALUES (%s, %s, 'coordinator', 'active')",
        (email.strip().lower(), generate_password_hash(password, method=PASSWORD_HASH_METHOD)),
    )
    log.info("Seeded coordinator account.")

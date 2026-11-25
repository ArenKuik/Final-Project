"""Checks that startup creates the schema and seeds exactly one coordinator."""
import os

from volunteerconnect import create_app
from volunteerconnect.db import get_conn

TABLES = {
    "users", "schools", "clearances", "help_requests",
    "shifts", "signups", "swap_requests", "strikes",
}


def _fetch(sql, params=()):
    with get_conn(os.environ["DATABASE_URL"]) as conn:
        return conn.execute(sql, params).fetchall()


def test_all_eight_tables_exist():
    create_app()
    rows = _fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )
    found = {row["table_name"] for row in rows}
    assert TABLES <= found, f"missing tables: {TABLES - found}"


def test_coordinator_seeded_exactly_once():
    # Starting the app twice re-runs schema.sql and the seed step.
    # That must not fail and must not create a second coordinator.
    create_app()
    create_app()
    rows = _fetch("SELECT count(*) AS n FROM users WHERE role = 'coordinator'")
    assert rows[0]["n"] == 1


def test_coordinator_password_is_hashed():
    create_app()
    rows = _fetch("SELECT password_hash FROM users WHERE role = 'coordinator'")
    assert rows, "no coordinator found"
    stored = rows[0]["password_hash"]
    assert stored != os.environ.get("COORDINATOR_PASSWORD")
    assert stored.startswith(("scrypt:", "pbkdf2:"))

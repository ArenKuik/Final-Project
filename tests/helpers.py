"""Helpers shared by the database tests.

Test data is easy to find and delete:
  - users have emails like  test-<random>@example.test
  - schools have names like  TEST <random>  (or invite codes starting TEST-)
"""
import os
import uuid
from datetime import date, timedelta

import psycopg
from psycopg.rows import dict_row
from werkzeug.security import generate_password_hash

from volunteerconnect.db import PASSWORD_HASH_METHOD

PASSWORD = "correct-horse-battery"
_HASH = generate_password_hash(PASSWORD, method=PASSWORD_HASH_METHOD)


_conn = None


def run_sql(sql, params=()):
    """Run one statement against the test database and return any rows.
    Reuses one autocommit connection, because connecting to Neon is slow."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, autocommit=True)
    cur = _conn.execute(sql, params)
    return cur.fetchall() if cur.description else []


def one(sql, params=()):
    rows = run_sql(sql, params)
    return rows[0] if rows else None


def new_email():
    return f"test-{uuid.uuid4().hex[:12]}@example.test"


def random_school_name():
    return f"TEST {uuid.uuid4().hex[:8]}"


def future(days=365):
    return date.today() + timedelta(days=days)


def past(days=30):
    return date.today() - timedelta(days=days)


def make_school(required=()):
    """Insert a school directly; returns the row."""
    return one(
        "INSERT INTO schools (name, invite_code, required_clearances) "
        "VALUES (%s, %s, %s) RETURNING *",
        (random_school_name(), f"TEST-{uuid.uuid4().hex[:10]}", list(required)),
    )


def make_request(school_id, teacher_id, status="submitted", day=None,
                 start="09:00", end="11:00", needed=2, note=None):
    """Insert a help request directly; returns the row."""
    return one(
        "INSERT INTO help_requests (school_id, teacher_id, activity, room, date, start_time, "
        "end_time, volunteers_needed, status, coordinator_note) "
        "VALUES (%s, %s, 'TEST activity', 'Room 1', %s, %s, %s, %s, %s, %s) RETURNING *",
        (school_id, teacher_id, day or future(30), start, end, needed, status, note),
    )


def make_shift(school_id, request_id=None, status="open"):
    """Insert a shift directly (30 days ahead, 09:00-11:00 LA time); returns the row."""
    day = future(30)
    return one(
        "INSERT INTO shifts (request_id, school_id, start_at, end_at, capacity, status) VALUES "
        "(%s, %s, (%s::date + '09:00'::time) AT TIME ZONE 'America/Los_Angeles', "
        "(%s::date + '11:00'::time) AT TIME ZONE 'America/Los_Angeles', 2, %s) RETURNING *",
        (request_id, school_id, day, day, status),
    )


def make_user(role, school_id=None, status=None):
    """Insert a user directly (no web request); returns the row."""
    status = status or ("pending" if role == "volunteer" else "active")
    return one(
        "INSERT INTO users (email, password_hash, role, school_id, status) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING *",
        (new_email(), _HASH, role, school_id, status),
    )


def coordinator_id():
    return one("SELECT id FROM users WHERE role = 'coordinator' ORDER BY id LIMIT 1")["id"]


def login_as(client, user_id):
    """Log in by setting the session directly (fast; no password needed)."""
    with client.session_transaction() as sess:
        sess.clear()
        sess["user_id"] = user_id


def delete_test_data():
    """Remove everything the tests created. Order matters because of foreign keys."""
    run_sql("DELETE FROM shifts WHERE school_id IN "
            "(SELECT id FROM schools WHERE name LIKE 'TEST %%' OR invite_code LIKE 'TEST-%%')")
    run_sql("DELETE FROM help_requests WHERE school_id IN "
            "(SELECT id FROM schools WHERE name LIKE 'TEST %%' OR invite_code LIKE 'TEST-%%')")
    run_sql("DELETE FROM clearances WHERE volunteer_id IN "
            "(SELECT id FROM users WHERE email LIKE 'test-%%@example.test')")
    run_sql("DELETE FROM users WHERE email LIKE 'test-%%@example.test'")
    run_sql("DELETE FROM schools WHERE name LIKE 'TEST %%' OR invite_code LIKE 'TEST-%%'")


def counts():
    """Row counts used to prove a forbidden request changed nothing."""
    return one(
        "SELECT (SELECT count(*) FROM schools) AS schools, "
        "(SELECT count(*) FROM clearances) AS clearances, "
        "(SELECT string_agg(invite_code || name || coalesce(array_to_string(required_clearances, ','), ''), '|' ORDER BY id) FROM schools) AS school_data, "
        "(SELECT string_agg(type || expires_on::text, '|' ORDER BY id) FROM clearances) AS clearance_data, "
        "(SELECT string_agg(status || coalesce(coordinator_note, '') || activity || date::text, '|' ORDER BY id) FROM help_requests) AS request_data, "
        "(SELECT string_agg(status || capacity::text || start_at::text, '|' ORDER BY id) FROM shifts) AS shift_data"
    )


def assert_forbidden_everywhere(app, client, blueprint, url_args, post_data):
    """As the currently logged-in (non-coordinator) user, every route in the
    blueprint must return 403 for GET and for hand-built POSTs, and the
    database must not change."""
    before = counts()
    checked = 0
    for rule in app.url_map.iter_rules():
        if not rule.endpoint.startswith(blueprint + "."):
            continue
        with app.test_request_context():
            from flask import url_for
            url = url_for(rule.endpoint, **{arg: url_args[arg] for arg in rule.arguments})
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            resp = client.open(url, method=method, data=post_data if method == "POST" else None)
            assert resp.status_code == 403, f"{method} {url} returned {resp.status_code}"
            checked += 1
    assert checked > 0, f"no routes found for blueprint {blueprint}"
    assert counts() == before

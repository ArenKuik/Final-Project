"""Accounts: register, login, logout, login_required and role_required.

Every user these tests create has an email like test-<random>@example.test,
and every school has an invite code starting with TEST-. The `cleanup`
fixture deletes them after each test, even if the test fails.
"""
import os
import uuid

import pytest

from volunteerconnect import create_app
from volunteerconnect.db import get_conn
from volunteerconnect.decorators import role_required

PASSWORD = "correct-horse-battery"  # 21 characters
PUBLIC_ENDPOINTS = {"main.home", "auth.login", "auth.register", "static"}


# ---------- helpers ----------

def run_sql(sql, params=()):
    """Run one statement against the test database and return any rows."""
    with get_conn(os.environ["DATABASE_URL"]) as conn:
        cur = conn.execute(sql, params)
        return cur.fetchall() if cur.description else []


def new_email():
    return f"test-{uuid.uuid4().hex[:12]}@example.test"


def get_user(email):
    rows = run_sql("SELECT * FROM users WHERE email = %s", (email,))
    return rows[0] if rows else None


def make_school():
    """Create a throwaway school and return (id, invite_code)."""
    code = f"TEST-{uuid.uuid4().hex[:10]}"
    rows = run_sql(
        "INSERT INTO schools (name, invite_code) VALUES (%s, %s) RETURNING id",
        ("Test School", code),
    )
    return rows[0]["id"], code


def register(client, email, role="volunteer", invite_code="", password=PASSWORD):
    return client.post("/register", data={
        "email": email, "password": password, "role": role, "invite_code": invite_code,
    })


def login(client, email, password=PASSWORD):
    return client.post("/login", data={"email": email, "password": password})


# ---------- fixtures ----------

@pytest.fixture(scope="module")
def app():
    app = create_app({"TESTING": True, "WTF_CSRF_ENABLED": False})

    # A route that only the coordinator may open, used to test role_required.
    @app.route("/_test/coordinator-only")
    @role_required("coordinator")
    def coordinator_only():
        return "ok"

    return app


@pytest.fixture
def client(app):
    client = app.test_client()
    # Pretend every request is HTTPS so the Secure session cookie is sent back.
    client.environ_base["wsgi.url_scheme"] = "https"
    return client


@pytest.fixture(autouse=True)
def cleanup():
    yield
    run_sql("DELETE FROM users WHERE email LIKE 'test-%%@example.test'")
    run_sql("DELETE FROM schools WHERE invite_code LIKE 'TEST-%%'")


# ---------- register ----------

def test_register_volunteer_is_pending_without_school(client):
    email = new_email()
    resp = register(client, email)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/login")

    user = get_user(email)
    assert user["role"] == "volunteer"
    assert user["status"] == "pending"
    assert user["school_id"] is None


def test_register_teacher_with_valid_invite_code(client):
    school_id, code = make_school()
    email = new_email()
    resp = register(client, email, role="teacher", invite_code=code)
    assert resp.status_code == 302

    user = get_user(email)
    assert user["role"] == "teacher"
    assert user["status"] == "active"
    assert user["school_id"] == school_id


def test_register_teacher_with_invalid_invite_code(client):
    email = new_email()
    resp = register(client, email, role="teacher", invite_code="TEST-does-not-exist")
    assert resp.status_code == 200
    assert b"doesn&#39;t match any school" in resp.data
    assert get_user(email) is None


def test_register_teacher_without_invite_code(client):
    email = new_email()
    resp = register(client, email, role="teacher", invite_code="")
    assert resp.status_code == 200
    assert get_user(email) is None


def test_cannot_register_as_coordinator(client):
    email = new_email()
    resp = register(client, email, role="coordinator")
    assert resp.status_code == 200
    assert b"Not a valid choice" in resp.data
    assert get_user(email) is None


def test_short_password_rejected(client):
    email = new_email()
    resp = register(client, email, password="short")
    assert resp.status_code == 200
    assert get_user(email) is None


def test_duplicate_email_rejected(client):
    email = new_email()
    register(client, email)
    resp = register(client, email)
    assert resp.status_code == 200
    assert b"already exists" in resp.data
    assert run_sql("SELECT count(*) AS n FROM users WHERE email = %s", (email,))[0]["n"] == 1


def test_password_stored_as_hash(client):
    email = new_email()
    register(client, email)
    stored = get_user(email)["password_hash"]
    assert stored != PASSWORD
    assert stored.startswith("pbkdf2:sha256")


# ---------- login / logout ----------

def test_login_with_right_password(client):
    email = new_email()
    register(client, email)
    resp = login(client, email)
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/dashboard")

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert email.encode() in page.data
    assert b"Volunteer" in page.data
    with client.session_transaction() as sess:
        assert list(sess.keys()) == ["user_id"]


def test_wrong_password_and_unknown_email_get_same_message(client):
    email = new_email()
    register(client, email)

    wrong_password = login(client, email, password="not-the-right-password")
    unknown_email = login(client, new_email())
    for resp in (wrong_password, unknown_email):
        assert resp.status_code == 200
        assert b"Invalid email or password." in resp.data
    with client.session_transaction() as sess:
        assert "user_id" not in sess


def test_logout_ends_session(client):
    email = new_email()
    register(client, email)
    login(client, email)
    assert client.get("/dashboard").status_code == 200

    resp = client.post("/logout")
    assert resp.status_code == 302
    with client.session_transaction() as sess:
        assert "user_id" not in sess
    assert client.get("/dashboard").status_code == 302


def test_logout_requires_post(client):
    assert client.get("/logout").status_code == 405


# ---------- permissions ----------

def test_logged_out_user_redirected_from_dashboard(client):
    resp = client.get("/dashboard")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")


def test_wrong_role_gets_403(client):
    email = new_email()
    register(client, email)
    login(client, email)
    resp = client.get("/_test/coordinator-only")
    assert resp.status_code == 403
    assert b"permission" in resp.data


def test_every_non_public_page_requires_login(app, client):
    """Catches a forgotten @login_required on any GET route, now or later."""
    for rule in app.url_map.iter_rules():
        if rule.endpoint in PUBLIC_ENDPOINTS or "GET" not in rule.methods or rule.arguments:
            continue
        resp = client.get(rule.rule)
        assert resp.status_code == 302, f"{rule.rule} is open to logged-out visitors"
        assert resp.headers["Location"].endswith("/login")


def test_post_without_csrf_token_is_rejected(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        resp = client.post("/login", data={"email": new_email(), "password": PASSWORD})
        assert resp.status_code == 400
    finally:
        app.config["WTF_CSRF_ENABLED"] = False

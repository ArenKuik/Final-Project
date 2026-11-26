"""Register, log in and log out.

The session stores only the user's id. Everything else about the user is
loaded fresh from the database on each request (see load_current_user).
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, session, url_for
from psycopg.errors import UniqueViolation
from werkzeug.security import check_password_hash, generate_password_hash

from .db import PASSWORD_HASH_METHOD, query, transaction
from .forms import LoginForm, LogoutForm, RegisterForm
from .volunteer_status import volunteer_statuses

bp = Blueprint("auth", __name__)

LOGIN_ERROR = "Invalid email or password."

# Rate limiting: after this many failed logins for one email within the
# window, that email is refused for the same length of time, even with the
# right password. A successful login clears the count.
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
LOCKED_ERROR = (f"Too many failed login attempts. Please wait {LOCKOUT_MINUTES} minutes "
                "and try again.")
# Any fixed number: the first half of the per-email advisory lock key.
LOGIN_LOCK_NAMESPACE = 2

# Used when someone logs in with an email that doesn't exist: we still check
# the password against this hash, so the response takes about as long as a
# real check and doesn't reveal which emails are registered.
_DUMMY_HASH = generate_password_hash("not-a-real-password", method=PASSWORD_HASH_METHOD)


def load_current_user():
    """Runs before every request: put the logged-in user (or None) in g.user."""
    g.user = None
    user_id = session.get("user_id")
    if user_id is None:
        return
    g.user = query(
        "SELECT id, email, role, school_id, status FROM users WHERE id = %s",
        (user_id,), one=True,
    )
    if g.user is None:
        # The account no longer exists; forget the stale session.
        session.clear()
    elif g.user["role"] == "volunteer":
        # A volunteer's status is derived from unexpired clearances, never
        # read from the stored column (see volunteer_status.py).
        g.user["status"] = volunteer_statuses([user_id])[user_id]


@bp.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("main.dashboard"))

    form = RegisterForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        role = form.role.data

        if query("SELECT 1 FROM users WHERE email = %s", (email,), one=True):
            form.email.errors.append("An account with this email already exists.")
            return render_template("register.html", form=form)

        if role == "teacher":
            # The invite code decides the teacher's school; they can't pick one.
            school = query(
                "SELECT id FROM schools WHERE invite_code = %s",
                (form.invite_code.data.strip(),), one=True,
            )
            if school is None:
                form.invite_code.errors.append("That invite code doesn't match any school.")
                return render_template("register.html", form=form)
            school_id, status = school["id"], "active"
        else:
            # Volunteers wait for the coordinator to record their clearances.
            school_id, status = None, "pending"

        try:
            query(
                "INSERT INTO users (email, password_hash, role, school_id, status) "
                "VALUES (%s, %s, %s, %s, %s)",
                (email, generate_password_hash(form.password.data, method=PASSWORD_HASH_METHOD),
                 role, school_id, status),
            )
        except UniqueViolation:
            # Two registrations with the same email at the same moment.
            form.email.errors.append("An account with this email already exists.")
            return render_template("register.html", form=form)

        flash("Account created. Please log in.", "success")
        return redirect(url_for("auth.login"))

    return render_template("register.html", form=form)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("main.dashboard"))

    form = LoginForm()
    if form.validate_on_submit():
        email = form.email.data.strip().lower()
        result = check_login(email, form.password.data)
        if result == "locked":
            # Same message whether or not the email has an account.
            form.form_errors.append(LOCKED_ERROR)
        elif result is None:
            # Same message whether the email or the password was wrong.
            form.form_errors.append(LOGIN_ERROR)
        else:
            # Start a fresh session (prevents session fixation), then
            # remember only the user's id.
            session.clear()
            session["user_id"] = result
            return redirect(url_for("main.dashboard"))

    return render_template("login.html", form=form)


def check_login(email, password):
    """Check a login attempt with rate limiting.

    Returns the user's id on success, None for a wrong email or password,
    or "locked" if this email has had too many recent failures.

    Everything runs in one transaction holding a lock for this email, so
    several guesses sent at the same moment can't get past the limit.
    """
    with transaction() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (LOGIN_LOCK_NAMESPACE, email))

        # Locked if the last MAX_FAILED_LOGINS failures all happened within
        # the window and the newest one was less than LOCKOUT_MINUTES ago.
        # So the lock lasts the full LOCKOUT_MINUTES after the last failure.
        cur.execute(
            """SELECT count(*) = %(max)s
                      AND max(attempted_at) > now() - make_interval(mins => %(mins)s)
                      AND min(attempted_at) > max(attempted_at) - make_interval(mins => %(mins)s)
                      AS locked
               FROM (SELECT attempted_at FROM login_attempts WHERE email = %(email)s
                     ORDER BY attempted_at DESC LIMIT %(max)s) AS recent""",
            {"email": email, "max": MAX_FAILED_LOGINS, "mins": LOCKOUT_MINUTES},
        )
        if cur.fetchone()["locked"]:
            return "locked"

        cur.execute("SELECT id, password_hash FROM users WHERE email = %s", (email,))
        user = cur.fetchone()
        stored_hash = user["password_hash"] if user else _DUMMY_HASH
        if user and check_password_hash(stored_hash, password):
            cur.execute("DELETE FROM login_attempts WHERE email = %s", (email,))  # reset the count
            return user["id"]

        # Record the failure, whether or not the email has an account.
        cur.execute("INSERT INTO login_attempts (email) VALUES (%s)", (email,))
        # Housekeeping: old attempts no longer matter.
        cur.execute("DELETE FROM login_attempts WHERE attempted_at < now() - interval '1 day'")
        return None


@bp.route("/logout", methods=["POST"])
def logout():
    if not LogoutForm().validate_on_submit():
        abort(400)
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("main.home"))

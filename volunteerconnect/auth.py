"""Register, log in and log out.

The session stores only the user's id. Everything else about the user is
loaded fresh from the database on each request (see load_current_user).
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, session, url_for
from psycopg.errors import UniqueViolation
from werkzeug.security import check_password_hash, generate_password_hash

from .db import PASSWORD_HASH_METHOD, query
from .forms import LoginForm, LogoutForm, RegisterForm

bp = Blueprint("auth", __name__)

LOGIN_ERROR = "Invalid email or password."

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
        user = query("SELECT id, password_hash FROM users WHERE email = %s", (email,), one=True)
        stored_hash = user["password_hash"] if user else _DUMMY_HASH
        password_ok = check_password_hash(stored_hash, form.password.data)

        if user and password_ok:
            # Start a fresh session (prevents session fixation), then
            # remember only the user's id.
            session.clear()
            session["user_id"] = user["id"]
            return redirect(url_for("main.dashboard"))

        # Same message whether the email or the password was wrong.
        form.form_errors.append(LOGIN_ERROR)

    return render_template("login.html", form=form)


@bp.route("/logout", methods=["POST"])
def logout():
    if not LogoutForm().validate_on_submit():
        abort(400)
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("main.home"))

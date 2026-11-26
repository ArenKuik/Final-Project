"""Coordinator: create and edit schools, and manage their invite codes."""
import secrets

from flask import Blueprint, abort, flash, redirect, render_template, url_for
from psycopg.errors import UniqueViolation

from .db import query
from .decorators import role_required
from .forms import CLEARANCE_TYPES, ActionForm, SchoolForm

bp = Blueprint("schools", __name__, url_prefix="/schools")

# No 0/O or 1/I, so codes are easy to read aloud and type.
INVITE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
INVITE_LENGTH = 10  # 32^10 ≈ 10^15 possible codes: not guessable
MAX_CODE_ATTEMPTS = 5


def generate_invite_code():
    """A random code from the `secrets` module (safe for security use)."""
    return "".join(secrets.choice(INVITE_ALPHABET) for _ in range(INVITE_LENGTH))


def run_with_new_code(sql, params_for_code):
    """Run an INSERT/UPDATE that sets invite_code, retrying if the random
    code happens to already exist (the column is UNIQUE)."""
    for _ in range(MAX_CODE_ATTEMPTS):
        try:
            return query(sql, params_for_code(generate_invite_code()), one=True)
        except UniqueViolation:
            continue
    raise RuntimeError("Could not generate a unique invite code")


def get_school_or_404(school_id):
    school = query("SELECT * FROM schools WHERE id = %s", (school_id,), one=True)
    if school is None:
        abort(404)
    return school


@bp.route("")
@role_required("coordinator")
def list_schools():
    schools = query("SELECT * FROM schools ORDER BY name")
    return render_template("schools/list.html", schools=schools,
                           action_form=ActionForm(), clearance_labels=dict(CLEARANCE_TYPES))


@bp.route("/new", methods=["GET", "POST"])
@role_required("coordinator")
def create_school():
    form = SchoolForm()
    if form.validate_on_submit():
        run_with_new_code(
            "INSERT INTO schools (name, address, invite_code, required_clearances) "
            "VALUES (%s, %s, %s, %s) RETURNING id",
            lambda code: (form.name.data.strip(), (form.address.data or "").strip() or None,
                          code, form.required_clearances.data),
        )
        flash(f"School “{form.name.data.strip()}” created.", "success")
        return redirect(url_for("schools.list_schools"))
    return render_template("schools/form.html", form=form, school=None)


@bp.route("/<int:school_id>/edit", methods=["GET", "POST"])
@role_required("coordinator")
def edit_school(school_id):
    school = get_school_or_404(school_id)
    # On GET the form shows the saved values; on POST the submitted values win.
    form = SchoolForm(data=school)
    if form.validate_on_submit():
        query(
            "UPDATE schools SET name = %s, address = %s, required_clearances = %s WHERE id = %s",
            (form.name.data.strip(), (form.address.data or "").strip() or None,
             form.required_clearances.data, school_id),
        )
        flash("School updated.", "success")
        return redirect(url_for("schools.list_schools"))
    return render_template("schools/form.html", form=form, school=school)


@bp.route("/<int:school_id>/regenerate-code", methods=["POST"])
@role_required("coordinator")
def regenerate_code(school_id):
    if not ActionForm().validate_on_submit():
        abort(400)
    get_school_or_404(school_id)
    # Only the code changes. Teachers already registered keep their
    # school_id, so they are not affected; the old code stops working.
    run_with_new_code(
        "UPDATE schools SET invite_code = %s WHERE id = %s RETURNING id",
        lambda code: (code, school_id),
    )
    flash("New invite code generated. The old code no longer works.", "success")
    return redirect(url_for("schools.list_schools"))

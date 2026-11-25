"""Volunteer sign-ups, plus the shift page for the coordinator and teachers.

The rules themselves live in signup_rules.py; these routes only check who
is asking, open a transaction, call a rule function and show the result.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from . import attendance_rules
from . import signup_rules as rules
from .db import query, transaction
from .decorators import role_required
from .forms import ActionForm

bp = Blueprint("signups", __name__)


def run_waitlist_expiry():
    """Rule 12, in its own short transaction before anything else, so it
    never holds locks while the main action runs."""
    with transaction() as cur:
        rules.expire_waitlists(cur)


# ---------- volunteer ----------

def render_shift_list(status=200):
    vol = g.user
    # Shifts this volunteer may see: open/full shifts that haven't started at
    # schools whose every required clearance they hold (unexpired on the
    # shift date) -- the same test as rules.check_clearance -- plus any shift
    # they are already confirmed or waitlisted on.
    shifts = query(
        """SELECT sh.id, sh.capacity, sh.status, s.name AS school_name,
                  hr.activity, hr.room,
                  sh.start_at AT TIME ZONE 'America/Los_Angeles' AS start_local,
                  sh.end_at AT TIME ZONE 'America/Los_Angeles' AS end_local,
                  (SELECT count(*) FROM signups x WHERE x.shift_id = sh.id
                     AND x.status IN ('confirmed', 'present', 'no_show')) AS taken,
                  my.id AS my_signup_id, my.status AS my_status,
                  (SELECT count(*) FROM signups w WHERE w.shift_id = sh.id AND w.status = 'waitlisted'
                     AND (w.created_at, w.id) <= (my.created_at, my.id)) AS my_position
           FROM shifts sh
           JOIN schools s ON s.id = sh.school_id
           LEFT JOIN help_requests hr ON hr.id = sh.request_id
           LEFT JOIN signups my ON my.shift_id = sh.id AND my.volunteer_id = %(vol)s
           WHERE my.status IN ('confirmed', 'waitlisted')
              OR (%(active)s
                  AND sh.status IN ('open', 'full')
                  AND sh.start_at > now()
                  AND NOT EXISTS (
                      SELECT 1 FROM unnest(s.required_clearances) AS req(type)
                      WHERE NOT EXISTS (
                          SELECT 1 FROM clearances c
                          WHERE c.volunteer_id = %(vol)s AND c.type = req.type
                            AND c.expires_on >= (sh.start_at AT TIME ZONE 'America/Los_Angeles')::date)))
           ORDER BY sh.start_at""",
        {"vol": vol["id"], "active": vol["status"] == "active"},
    )
    return render_template("shifts/list.html", shifts=shifts, action_form=ActionForm()), status


@bp.route("/shifts")
@role_required("volunteer")
def list_shifts():
    run_waitlist_expiry()
    return render_shift_list()


@bp.route("/shifts/<int:shift_id>/signup", methods=["POST"])
@role_required("volunteer")
def sign_up(shift_id):
    if not ActionForm().validate_on_submit():
        abort(400)
    run_waitlist_expiry()
    try:
        with transaction() as cur:
            signup_id, status = rules.sign_up(cur, g.user["id"], shift_id)
            position = rules.waitlist_position(cur, signup_id) if status == "waitlisted" else None
    except rules.SignupRefused as refused:
        flash(str(refused), "error")
        return render_shift_list(status=400)

    if status == "confirmed":
        flash("You're signed up. See you there!", "success")
    else:
        flash(f"This shift is full, so you're on the waitlist (#{position} in line). "
              "You'll be moved in automatically if a spot opens.", "success")
    return redirect(url_for("signups.my_signups"))


def render_my_signups(status=200):
    signups = query(
        """SELECT su.id, su.status, s.name AS school_name, hr.activity, hr.room,
                  sh.status AS shift_status,
                  sh.start_at AT TIME ZONE 'America/Los_Angeles' AS start_local,
                  sh.end_at AT TIME ZONE 'America/Los_Angeles' AS end_local,
                  sh.start_at <= now() AS started,
                  (SELECT count(*) FROM signups w WHERE w.shift_id = su.shift_id AND w.status = 'waitlisted'
                     AND (w.created_at, w.id) <= (su.created_at, su.id)) AS position
           FROM signups su
           JOIN shifts sh ON sh.id = su.shift_id
           JOIN schools s ON s.id = sh.school_id
           LEFT JOIN help_requests hr ON hr.id = sh.request_id
           WHERE su.volunteer_id = %s
           ORDER BY sh.start_at DESC""",
        (g.user["id"],),
    )
    return render_template("signups/mine.html", signups=signups, action_form=ActionForm()), status


@bp.route("/my-signups")
@role_required("volunteer")
def my_signups():
    run_waitlist_expiry()
    return render_my_signups()


@bp.route("/signups/<int:signup_id>/cancel", methods=["POST"])
@role_required("volunteer")
def cancel(signup_id):
    if not ActionForm().validate_on_submit():
        abort(400)
    owner = query("SELECT volunteer_id FROM signups WHERE id = %s", (signup_id,), one=True)
    if owner is None:
        abort(404)
    if owner["volunteer_id"] != g.user["id"]:
        abort(403)  # volunteers may only cancel their own sign-ups

    run_waitlist_expiry()
    try:
        with transaction() as cur:
            result = rules.cancel_signup(cur, signup_id)
    except rules.SignupRefused as refused:
        flash(str(refused), "error")
        return render_my_signups(status=400)

    if result["late"]:
        flash(f"Sign-up cancelled. Because the shift starts within {rules.LATE_CANCEL_HOURS} hours, "
              "this counts as a late cancel.", "error")
    else:
        flash("Sign-up cancelled.", "success")
    return redirect(url_for("signups.my_signups"))


# ---------- coordinator and teachers: one shift's sign-ups ----------

def render_shift_detail(shift_id, status=200):
    shift = query(
        """SELECT sh.*, s.name AS school_name, hr.activity, hr.room,
                  sh.start_at <= now() AS started,
                  sh.start_at AT TIME ZONE 'America/Los_Angeles' AS start_local,
                  sh.end_at AT TIME ZONE 'America/Los_Angeles' AS end_local
           FROM shifts sh JOIN schools s ON s.id = sh.school_id
           LEFT JOIN help_requests hr ON hr.id = sh.request_id
           WHERE sh.id = %s""",
        (shift_id,), one=True,
    )
    if shift is None:
        abort(404)
    # Teachers only see their own school's shifts.
    if g.user["role"] == "teacher" and shift["school_id"] != g.user["school_id"]:
        abort(403)

    signups = query(
        """SELECT su.id, su.status, su.created_at, u.email
           FROM signups su JOIN users u ON u.id = su.volunteer_id
           WHERE su.shift_id = %s AND su.status <> 'cancelled'
           ORDER BY su.created_at, su.id""",
        (shift_id,),
    )
    confirmed = [s for s in signups if s["status"] != "waitlisted"]
    waitlist = [s for s in signups if s["status"] == "waitlisted"]
    # Attendance is open from the start until the coordinator closes the shift (rule 10).
    can_mark = shift["started"] and shift["status"] not in ("cancelled", "completed")
    unmarked = sum(1 for su in confirmed if su["status"] == "confirmed")
    return render_template("shifts/detail.html", shift=shift, confirmed=confirmed,
                           waitlist=waitlist, action_form=ActionForm(),
                           can_mark=can_mark, unmarked=unmarked), status


@bp.route("/shifts/<int:shift_id>")
@role_required("coordinator", "teacher")
def shift_detail(shift_id):
    run_waitlist_expiry()
    return render_shift_detail(shift_id)


# ---------- attendance (rules 9 and 10) ----------

@bp.route("/signups/<int:signup_id>/attendance", methods=["POST"])
@role_required("teacher", "coordinator")
def mark_attendance(signup_id):
    if not ActionForm().validate_on_submit():
        abort(400)
    outcome = request.form.get("outcome")
    if outcome not in attendance_rules.OUTCOMES:
        abort(400)
    found = query(
        "SELECT sh.id AS shift_id, sh.school_id FROM signups su JOIN shifts sh ON sh.id = su.shift_id "
        "WHERE su.id = %s",
        (signup_id,), one=True,
    )
    if found is None:
        abort(404)
    # Teachers may only mark their own school's shifts.
    if g.user["role"] == "teacher" and found["school_id"] != g.user["school_id"]:
        abort(403)

    try:
        with transaction() as cur:
            attendance_rules.mark(cur, signup_id, outcome)
    except attendance_rules.AttendanceRefused as refused:
        flash(str(refused), "error")
        return render_shift_detail(found["shift_id"], status=400)
    flash("Marked present." if outcome == "present" else "Marked as a no-show.", "success")
    return redirect(url_for("signups.shift_detail", shift_id=found["shift_id"]))


@bp.route("/shifts/<int:shift_id>/complete", methods=["POST"])
@role_required("coordinator")
def complete_shift(shift_id):
    if not ActionForm().validate_on_submit():
        abort(400)
    try:
        with transaction() as cur:
            attendance_rules.close_shift(cur, shift_id)
    except attendance_rules.AttendanceRefused as refused:
        flash(str(refused), "error")
        return render_shift_detail(shift_id, status=400)
    flash("Shift closed. Hours for volunteers marked present now count.", "success")
    return redirect(url_for("signups.shift_detail", shift_id=shift_id))


@bp.route("/signups/<int:signup_id>/remove", methods=["POST"])
@role_required("coordinator")
def remove(signup_id):
    if not ActionForm().validate_on_submit():
        abort(400)
    run_waitlist_expiry()
    shift = query("SELECT shift_id FROM signups WHERE id = %s", (signup_id,), one=True)
    if shift is None:
        abort(404)
    try:
        with transaction() as cur:
            result = rules.cancel_signup(cur, signup_id, by_coordinator=True)
    except rules.SignupRefused as refused:
        flash(str(refused), "error")
    else:
        message = "Volunteer removed."
        if result["promoted"]:
            message += f" {result['promoted']} was moved in from the waitlist."
        flash(message, "success")
    return redirect(url_for("signups.shift_detail", shift_id=shift["shift_id"]))

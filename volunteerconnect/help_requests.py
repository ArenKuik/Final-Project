"""Help requests (teachers) and turning them into shifts (coordinator).

Request status moves only along these arrows (PRD, "Statuses"):

    submitted ──► approved
        │    └──► rejected
        └──► changes_requested ──► submitted   (the teacher edits and resubmits)

Every status change runs in one transaction that first locks the request
row (SELECT ... FOR UPDATE), so two people clicking at the same moment
can't both change it.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, request, url_for

from .db import query, today_la, transaction
from .decorators import role_required
from .forms import (ActionForm, ApproveForm, CoordinatorHelpRequestForm, HelpRequestForm,
                    RejectForm, SendBackForm)

bp = Blueprint("help_requests", __name__)

STATUSES = ["submitted", "changes_requested", "approved", "rejected"]
ALLOWED_MOVES = {
    "submitted": {"approved", "rejected", "changes_requested"},
    "changes_requested": {"submitted"},
    # approved and rejected are final.
}
CANCELLABLE_SHIFT_STATUSES = {"open", "full"}


def can_move(current, new):
    return new in ALLOWED_MOVES.get(current, set())


def check_school_access(help_request):
    """Teachers may only touch their own school's requests."""
    if g.user["role"] == "teacher" and help_request["school_id"] != g.user["school_id"]:
        abort(403)


def get_request_for_user(request_id):
    """Load a request for the current user: 404 if it doesn't exist, 403 if
    it belongs to another school."""
    help_request = query(
        """SELECT hr.*, s.name AS school_name, u.email AS teacher_email
           FROM help_requests hr
           JOIN schools s ON s.id = hr.school_id
           JOIN users u ON u.id = hr.teacher_id
           WHERE hr.id = %s""",
        (request_id,), one=True,
    )
    if help_request is None:
        abort(404)
    check_school_access(help_request)
    return help_request


def lock_request(cur, request_id):
    """Inside a transaction: lock the request row until the transaction ends."""
    cur.execute("SELECT * FROM help_requests WHERE id = %s FOR UPDATE", (request_id,))
    help_request = cur.fetchone()
    if help_request is None:
        abort(404)
    return help_request


def require_move(help_request, new_status):
    """Refuse a status change that isn't allowed (409 Conflict)."""
    if not can_move(help_request["status"], new_status):
        abort(409, description=(
            f"This request is {help_request['status'].replace('_', ' ')}, "
            f"so it can't be changed to {new_status.replace('_', ' ')}."))


def reject_past_date(field, today):
    """Add an error if a date field is before today (Los Angeles). Returns False if so."""
    if field.data and field.data < today:
        field.errors.append("The date can't be in the past.")
        return False
    return True


def shifts_for(request_id):
    return query(
        """SELECT id, start_at AT TIME ZONE 'America/Los_Angeles' AS start_local,
                  end_at AT TIME ZONE 'America/Los_Angeles' AS end_local,
                  capacity, status
           FROM shifts WHERE request_id = %s ORDER BY start_at""",
        (request_id,),
    )


# ---------- lists and details ----------

@bp.route("/requests")
@role_required("teacher", "coordinator")
def list_requests():
    if g.user["role"] == "teacher":
        status, school_id = None, g.user["school_id"]
    else:
        # Coordinator filters. Unknown values are ignored rather than trusted.
        status = request.args.get("status")
        status = status if status in STATUSES else None
        school_id = request.args.get("school", type=int)

    # NULL filters match everything, so one fixed query covers every case.
    requests_ = query(
        """SELECT hr.*, s.name AS school_name,
                  (SELECT count(*) FROM shifts sh WHERE sh.request_id = hr.id) AS shift_count
           FROM help_requests hr JOIN schools s ON s.id = hr.school_id
           WHERE (%(status)s::text IS NULL OR hr.status = %(status)s)
             AND (%(school)s::bigint IS NULL OR hr.school_id = %(school)s)
           ORDER BY hr.date, hr.start_time""",
        {"status": status, "school": school_id},
    )
    schools = query("SELECT id, name FROM schools ORDER BY name") if g.user["role"] == "coordinator" else []
    return render_template("requests/list.html", requests=requests_, schools=schools,
                           statuses=STATUSES, status_filter=status, school_filter=school_id)


def render_detail(help_request, approve_form=None, send_back_form=None, reject_form=None, status=200):
    if approve_form is None:
        # Start with one shift that matches the request exactly.
        approve_form = ApproveForm(formdata=None, data={"shifts": [{
            "date": help_request["date"], "start_time": help_request["start_time"],
            "end_time": help_request["end_time"], "capacity": help_request["volunteers_needed"],
        }]})
    return render_template(
        "requests/detail.html", req=help_request, shifts=shifts_for(help_request["id"]),
        approve_form=approve_form,
        send_back_form=send_back_form or SendBackForm(formdata=None),
        reject_form=reject_form or RejectForm(formdata=None),
        action_form=ActionForm(), can_move=can_move,
    ), status


@bp.route("/requests/<int:request_id>")
@role_required("teacher", "coordinator")
def request_detail(request_id):
    return render_detail(get_request_for_user(request_id))


# ---------- teacher: submit and resubmit ----------

@bp.route("/requests/new", methods=["GET", "POST"])
@role_required("teacher", "coordinator")
def new_request():
    if g.user["role"] == "coordinator":
        # The coordinator picks any existing school.
        form = CoordinatorHelpRequestForm()
        form.school_id.choices = [(0, "Choose a school…")] + [
            (s["id"], s["name"]) for s in query("SELECT id, name FROM schools ORDER BY name")]
    else:
        # Teachers get a form with no school field at all.
        form = HelpRequestForm()

    if form.validate_on_submit() and reject_past_date(form.date, today_la()):
        # A teacher's school always comes from their account, never from the
        # form (even if someone posts a school_id by hand).
        if g.user["role"] == "coordinator":
            school_id = form.school_id.data
        else:
            school_id = g.user["school_id"]
        row = query(
            """INSERT INTO help_requests
                   (school_id, teacher_id, activity, room, date, start_time, end_time,
                    volunteers_needed, status)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'submitted') RETURNING id""",
            # teacher_id records who asked (the coordinator, for their own requests).
            (school_id, g.user["id"], form.activity.data.strip(),
             (form.room.data or "").strip() or None, form.date.data,
             form.start_time.data, form.end_time.data, form.volunteers_needed.data),
            one=True,
        )
        flash("Help request submitted.", "success")
        return redirect(url_for("help_requests.request_detail", request_id=row["id"]))
    return render_template("requests/form.html", form=form, req=None)


@bp.route("/requests/<int:request_id>/edit", methods=["GET", "POST"])
@role_required("teacher")
def edit_request(request_id):
    help_request = get_request_for_user(request_id)
    if help_request["status"] != "changes_requested":
        abort(403)

    form = HelpRequestForm(data=help_request)
    if form.validate_on_submit() and reject_past_date(form.date, today_la()):
        with transaction() as cur:
            locked = lock_request(cur, request_id)
            if locked["status"] != "changes_requested":
                abort(403)  # changed by someone else since the page loaded
            cur.execute(
                """UPDATE help_requests
                   SET activity = %s, room = %s, date = %s, start_time = %s, end_time = %s,
                       volunteers_needed = %s, status = 'submitted'
                   WHERE id = %s""",
                (form.activity.data.strip(), (form.room.data or "").strip() or None,
                 form.date.data, form.start_time.data, form.end_time.data,
                 form.volunteers_needed.data, request_id),
            )
        flash("Request resubmitted.", "success")
        return redirect(url_for("help_requests.request_detail", request_id=request_id))
    return render_template("requests/form.html", form=form, req=help_request)


# ---------- coordinator: approve, send back, reject ----------

@bp.route("/requests/<int:request_id>/approve", methods=["POST"])
@role_required("coordinator")
def approve_request(request_id):
    help_request = get_request_for_user(request_id)
    form = ApproveForm()
    valid = form.validate_on_submit()
    if valid:
        today = today_la()
        for row in form.shifts:
            valid = reject_past_date(row.form.date, today) and valid
    if not valid:
        return render_detail(help_request, approve_form=form, status=400)

    with transaction() as cur:
        locked = lock_request(cur, request_id)
        require_move(locked, "approved")
        for row in form.shifts:
            shift = row.form
            # (date + time) AT TIME ZONE 'America/Los_Angeles' turns a wall-clock
            # time in LA into a timestamptz; Postgres handles PST/PDT.
            cur.execute(
                """INSERT INTO shifts (request_id, school_id, start_at, end_at, capacity, status)
                   VALUES (%s, %s,
                           (%s::date + %s::time) AT TIME ZONE 'America/Los_Angeles',
                           (%s::date + %s::time) AT TIME ZONE 'America/Los_Angeles',
                           %s, 'open')""",
                (request_id, locked["school_id"], shift.date.data, shift.start_time.data,
                 shift.date.data, shift.end_time.data, shift.capacity.data),
            )
        cur.execute("UPDATE help_requests SET status = 'approved' WHERE id = %s", (request_id,))

    count = len(form.shifts)
    flash(f"Request approved: {count} shift{'s' if count != 1 else ''} created.", "success")
    return redirect(url_for("help_requests.request_detail", request_id=request_id))


@bp.route("/requests/<int:request_id>/send-back", methods=["POST"])
@role_required("coordinator")
def send_back_request(request_id):
    help_request = get_request_for_user(request_id)
    form = SendBackForm()
    if not form.validate_on_submit():
        return render_detail(help_request, send_back_form=form, status=400)

    with transaction() as cur:
        locked = lock_request(cur, request_id)
        require_move(locked, "changes_requested")
        cur.execute(
            "UPDATE help_requests SET status = 'changes_requested', coordinator_note = %s WHERE id = %s",
            (form.note.data.strip(), request_id),
        )
    flash("Request sent back to the teacher.", "success")
    return redirect(url_for("help_requests.request_detail", request_id=request_id))


@bp.route("/requests/<int:request_id>/reject", methods=["POST"])
@role_required("coordinator")
def reject_request(request_id):
    help_request = get_request_for_user(request_id)
    form = RejectForm()
    if not form.validate_on_submit():
        return render_detail(help_request, reject_form=form, status=400)

    with transaction() as cur:
        locked = lock_request(cur, request_id)
        require_move(locked, "rejected")
        # Keep the previous note if no reason is given.
        cur.execute(
            "UPDATE help_requests SET status = 'rejected', "
            "coordinator_note = COALESCE(%s, coordinator_note) WHERE id = %s",
            ((form.reject_note.data or "").strip() or None, request_id),
        )
    flash("Request rejected.", "success")
    return redirect(url_for("help_requests.request_detail", request_id=request_id))


# ---------- coordinator: cancel a shift ----------

@bp.route("/shifts/<int:shift_id>/cancel", methods=["POST"])
@role_required("coordinator")
def cancel_shift(shift_id):
    if not ActionForm().validate_on_submit():
        abort(400)
    with transaction() as cur:
        cur.execute("SELECT * FROM shifts WHERE id = %s FOR UPDATE", (shift_id,))
        shift = cur.fetchone()
        if shift is None:
            abort(404)
        if shift["status"] not in CANCELLABLE_SHIFT_STATUSES:
            abort(409, description=f"This shift is already {shift['status']}.")
        # TODO (sign-ups milestone): also cancel this shift's sign-ups.
        cur.execute("UPDATE shifts SET status = 'cancelled' WHERE id = %s", (shift_id,))

    flash("Shift cancelled.", "success")
    if shift["request_id"]:
        return redirect(url_for("help_requests.request_detail", request_id=shift["request_id"]))
    return redirect(url_for("help_requests.list_requests"))

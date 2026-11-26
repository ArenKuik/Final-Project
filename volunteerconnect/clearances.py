"""Coordinator: record and revoke volunteer clearances.

A volunteer is 'active' while they hold at least one unexpired clearance,
and 'pending' otherwise. That status is always derived when it is shown
(see volunteer_status.py), so it is right even after a clearance quietly
expires.
"""
from flask import Blueprint, abort, flash, g, redirect, render_template, url_for

from .db import query, today_la, transaction
from .decorators import role_required
from .forms import CLEARANCE_TYPES, ActionForm, ClearanceForm
from .volunteer_status import volunteer_statuses

bp = Blueprint("volunteers", __name__, url_prefix="/volunteers")


def refresh_volunteer_status(cur, volunteer_id):
    """Keep the stored users.status roughly in step after a record/revoke.

    The stored column is NOT authoritative for volunteers (it goes stale when
    a clearance expires on its own); pages use volunteer_statuses() instead.
    It is still written because code on later branches reads it until those
    branches switch to the helper too."""
    cur.execute(
        """UPDATE users SET status = CASE
                WHEN EXISTS (SELECT 1 FROM clearances
                             WHERE volunteer_id = %s
                               AND expires_on >= (now() AT TIME ZONE 'America/Los_Angeles')::date)
                THEN 'active' ELSE 'pending' END
            WHERE id = %s AND role = 'volunteer'""",
        (volunteer_id, volunteer_id),
    )


def get_volunteer_or_404(volunteer_id):
    volunteer = query(
        "SELECT id, email, status FROM users WHERE id = %s AND role = 'volunteer'",
        (volunteer_id,), one=True,
    )
    if volunteer is None:
        abort(404)
    volunteer["status"] = volunteer_statuses([volunteer_id])[volunteer_id]  # derived, not stored
    return volunteer


def clearances_for(volunteer_id):
    return query(
        """SELECT id, type, expires_on, verified_at,
                  expires_on < (now() AT TIME ZONE 'America/Los_Angeles')::date AS expired
            FROM clearances WHERE volunteer_id = %s ORDER BY type""",
        (volunteer_id,),
    )


@bp.route("")
@role_required("coordinator")
def list_volunteers():
    volunteers = query("SELECT id, email FROM users WHERE role = 'volunteer' ORDER BY email")
    statuses = volunteer_statuses([v["id"] for v in volunteers])  # derived, not stored
    for v in volunteers:
        v["status"] = statuses[v["id"]]
    rows = query(
        """SELECT c.volunteer_id, c.type, c.expires_on,
                  c.expires_on < (now() AT TIME ZONE 'America/Los_Angeles')::date AS expired
            FROM clearances c JOIN users u ON u.id = c.volunteer_id
            WHERE u.role = 'volunteer' ORDER BY c.type"""
    )
    by_volunteer = {}
    for row in rows:
        by_volunteer.setdefault(row["volunteer_id"], []).append(row)
    return render_template("volunteers/list.html", volunteers=volunteers,
                           clearances=by_volunteer, labels=dict(CLEARANCE_TYPES))


def render_detail(volunteer, form, status=200):
    return render_template(
        "volunteers/detail.html", volunteer=volunteer, form=form,
        clearances=clearances_for(volunteer["id"]), action_form=ActionForm(),
        labels=dict(CLEARANCE_TYPES),
    ), status


@bp.route("/<int:volunteer_id>")
@role_required("coordinator")
def volunteer_detail(volunteer_id):
    return render_detail(get_volunteer_or_404(volunteer_id), ClearanceForm())


@bp.route("/<int:volunteer_id>/clearances", methods=["POST"])
@role_required("coordinator")
def record_clearance(volunteer_id):
    volunteer = get_volunteer_or_404(volunteer_id)
    form = ClearanceForm()
    valid = form.validate_on_submit()
    if valid and form.expires_on.data < today_la():
        form.expires_on.errors.append("Expiry date can't be in the past.")
        valid = False
    if not valid:
        return render_detail(volunteer, form, status=400)

    with transaction() as cur:
        # One row per type per volunteer: recording the same type again
        # updates the expiry instead of adding a second row.
        cur.execute(
            """INSERT INTO clearances (volunteer_id, type, verified_by, verified_at, expires_on)
               VALUES (%s, %s, %s, now(), %s)
               ON CONFLICT (volunteer_id, type) DO UPDATE
               SET expires_on = EXCLUDED.expires_on,
                   verified_by = EXCLUDED.verified_by,
                   verified_at = now()""",
            (volunteer_id, form.type.data, g.user["id"], form.expires_on.data),
        )
        refresh_volunteer_status(cur, volunteer_id)

    flash("Clearance recorded.", "success")
    return redirect(url_for("volunteers.volunteer_detail", volunteer_id=volunteer_id))


@bp.route("/<int:volunteer_id>/clearances/<int:clearance_id>/revoke", methods=["POST"])
@role_required("coordinator")
def revoke_clearance(volunteer_id, clearance_id):
    if not ActionForm().validate_on_submit():
        abort(400)
    get_volunteer_or_404(volunteer_id)
    with transaction() as cur:
        # Matching on volunteer_id too means a clearance id from another
        # volunteer can't be revoked through this URL.
        cur.execute(
            "DELETE FROM clearances WHERE id = %s AND volunteer_id = %s RETURNING id",
            (clearance_id, volunteer_id),
        )
        if cur.fetchone() is None:
            abort(404)
        refresh_volunteer_status(cur, volunteer_id)

    flash("Clearance revoked.", "success")
    return redirect(url_for("volunteers.volunteer_detail", volunteer_id=volunteer_id))

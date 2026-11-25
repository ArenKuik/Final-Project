"""Hours and reports (PRD rule 9).

Hours are never stored. Every query below calculates them the same way as
attendance_rules.HOURS_BY_VOLUNTEER_SQL:

    hours = (shift end_at - start_at) in hours, for signups with status 'present'

so they always match attendance. Optional school filters use
"(%(school)s::bigint IS NULL OR ... = %(school)s)": passing None means
"all schools", so each query is one fixed SQL string.
"""
from flask import Blueprint, abort, g, render_template

from .db import query
from .decorators import role_required

bp = Blueprint("reports", __name__)


# ---------- queries ----------

def present_shifts(volunteer_id, school_id=None):
    """One volunteer's shifts marked present, newest first, with hours each."""
    return query(
        """SELECT sh.id, s.name AS school_name, hr.activity,
                  sh.start_at AT TIME ZONE 'America/Los_Angeles' AS start_local,
                  sh.end_at AT TIME ZONE 'America/Los_Angeles' AS end_local,
                  -- rule 9: end minus start, in hours
                  round((EXTRACT(EPOCH FROM sh.end_at - sh.start_at) / 3600)::numeric, 2) AS hours
           FROM signups su
           JOIN shifts sh ON sh.id = su.shift_id
           JOIN schools s ON s.id = sh.school_id
           LEFT JOIN help_requests hr ON hr.id = sh.request_id
           WHERE su.volunteer_id = %(vol)s
             AND su.status = 'present'
             AND (%(school)s::bigint IS NULL OR sh.school_id = %(school)s)
           ORDER BY sh.start_at DESC""",
        {"vol": volunteer_id, "school": school_id},
    )


def total_hours(volunteer_id=None, school_id=None):
    """Total present hours, optionally for one volunteer and/or one school."""
    row = query(
        """SELECT round(COALESCE(sum(EXTRACT(EPOCH FROM sh.end_at - sh.start_at) / 3600), 0)::numeric, 2)
                  AS hours  -- rule 9
           FROM signups su JOIN shifts sh ON sh.id = su.shift_id
           WHERE su.status = 'present'
             AND (%(vol)s::bigint IS NULL OR su.volunteer_id = %(vol)s)
             AND (%(school)s::bigint IS NULL OR sh.school_id = %(school)s)""",
        {"vol": volunteer_id, "school": school_id}, one=True,
    )
    return row["hours"]


def hours_by_school(school_id=None):
    """Every school (or just one), including schools with 0 hours."""
    return query(
        """SELECT s.id, s.name,
                  -- rule 9: only 'present' sign-ups count
                  round(COALESCE(sum(EXTRACT(EPOCH FROM sh.end_at - sh.start_at) / 3600)
                                 FILTER (WHERE su.status = 'present'), 0)::numeric, 2) AS hours,
                  count(su.id) FILTER (WHERE su.status = 'present') AS shifts,
                  count(DISTINCT su.volunteer_id) FILTER (WHERE su.status = 'present') AS volunteers
           FROM schools s
           LEFT JOIN shifts sh ON sh.school_id = s.id
           LEFT JOIN signups su ON su.shift_id = sh.id
           WHERE (%(school)s::bigint IS NULL OR s.id = %(school)s)
           GROUP BY s.id, s.name
           ORDER BY s.name""",
        {"school": school_id},
    )


def hours_by_volunteer(school_id=None):
    """Per-volunteer totals. With no school: every volunteer (0 if none).
    With a school: only volunteers who have had a sign-up there, counting
    only that school's shifts."""
    return query(
        """SELECT u.id, u.email,
                  -- rule 9: only 'present' sign-ups count
                  round(COALESCE(sum(EXTRACT(EPOCH FROM sh.end_at - sh.start_at) / 3600)
                                 FILTER (WHERE su.status = 'present'), 0)::numeric, 2) AS hours,
                  count(su.id) FILTER (WHERE su.status = 'present') AS shifts
           FROM users u
           LEFT JOIN signups su ON su.volunteer_id = u.id
           LEFT JOIN shifts sh ON sh.id = su.shift_id
           WHERE u.role = 'volunteer'
             AND (%(school)s::bigint IS NULL OR sh.school_id = %(school)s)
           GROUP BY u.id, u.email
           ORDER BY hours DESC, u.email""",
        {"school": school_id},
    )


def volunteer_had_shift_at(volunteer_id, school_id):
    """True if the volunteer has ever had a sign-up (any status) at this school."""
    return query(
        """SELECT 1 FROM signups su JOIN shifts sh ON sh.id = su.shift_id
           WHERE su.volunteer_id = %s AND sh.school_id = %s LIMIT 1""",
        (volunteer_id, school_id), one=True,
    ) is not None


# ---------- pages ----------

@bp.route("/my-hours")
@role_required("volunteer")
def my_hours():
    return render_template("reports/my_hours.html",
                           shifts=present_shifts(g.user["id"]),
                           total=total_hours(volunteer_id=g.user["id"]))


@bp.route("/hours/<int:volunteer_id>")
@role_required("coordinator", "teacher")
def volunteer_hours(volunteer_id):
    school_id = None
    if g.user["role"] == "teacher":
        # Teachers only see volunteers who have worked at their school, and
        # only those hours. Unknown ids get 403 too, so ids can't be probed.
        school_id = g.user["school_id"]
        if not volunteer_had_shift_at(volunteer_id, school_id):
            abort(403)

    volunteer = query("SELECT id, email FROM users WHERE id = %s AND role = 'volunteer'",
                      (volunteer_id,), one=True)
    if volunteer is None:
        abort(404)
    return render_template("reports/volunteer_hours.html", volunteer=volunteer,
                           shifts=present_shifts(volunteer_id, school_id),
                           total=total_hours(volunteer_id, school_id))


@bp.route("/reports")
@role_required("coordinator", "teacher")
def index():
    # Teachers: everything limited to their own school.
    school_id = g.user["school_id"] if g.user["role"] == "teacher" else None
    return render_template("reports/index.html",
                           schools=hours_by_school(school_id),
                           volunteers=hours_by_volunteer(school_id),
                           total=total_hours(school_id=school_id))

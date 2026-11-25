"""Attendance rules (PRD rules 9 and 10).

Like signup_rules.py, every function takes a cursor and runs inside the
caller's transaction. The shift row is locked first (SELECT ... FOR UPDATE)
so two people marking the same shift at once can't interfere.

Rule 9 (hours): hours are never stored. They are always calculated from
sign-ups marked present, so they can never disagree with attendance:

    hours = sum(shift end_at - start_at) over signups with status 'present'

HOURS_BY_VOLUNTEER_SQL below is that calculation; the hours pages will use it.
"""
from werkzeug.exceptions import NotFound

from .signup_rules import lock_shift

OUTCOMES = ("present", "no_show")
MARKABLE = ("confirmed", "present", "no_show")  # waitlisted/cancelled can't be marked

HOURS_BY_VOLUNTEER_SQL = """
    SELECT su.volunteer_id,
           COALESCE(sum(EXTRACT(EPOCH FROM sh.end_at - sh.start_at)) / 3600, 0) AS hours
    FROM signups su JOIN shifts sh ON sh.id = su.shift_id
    WHERE su.status = 'present' AND su.volunteer_id = ANY(%s)
    GROUP BY su.volunteer_id
"""


class AttendanceRefused(Exception):
    """A rule said no. The message is shown to the teacher or coordinator."""


def check_can_mark(cur, shift):
    """Attendance is open from the shift's start until the coordinator closes it."""
    if shift["status"] == "cancelled":
        return "This shift was cancelled."
    if shift["status"] == "completed":
        return "This shift is closed, so attendance can't be changed."
    if not shift["started"]:
        # Rule 10: only after the shift has started.
        cur.execute(
            "SELECT to_char(%s AT TIME ZONE 'America/Los_Angeles', 'Mon DD at HH24:MI') AS starts",
            (shift["start_at"],),
        )
        return ("Attendance can be recorded once the shift has started "
                f"({cur.fetchone()['starts']}).")
    return None


def mark(cur, signup_id, outcome):
    """Mark one confirmed volunteer present or no_show (can be changed later)."""
    if outcome not in OUTCOMES:
        raise ValueError(outcome)
    cur.execute("SELECT shift_id FROM signups WHERE id = %s", (signup_id,))
    row = cur.fetchone()
    if row is None:
        raise NotFound()
    shift = lock_shift(cur, row["shift_id"])
    cur.execute("SELECT * FROM signups WHERE id = %s FOR UPDATE", (signup_id,))
    signup = cur.fetchone()

    problem = check_can_mark(cur, shift)
    if problem:
        raise AttendanceRefused(problem)
    if signup["status"] not in MARKABLE:
        raise AttendanceRefused(f"Only confirmed volunteers can be marked "
                                f"(this sign-up is {signup['status']}).")

    cur.execute("UPDATE signups SET status = %s WHERE id = %s", (outcome, signup_id))

    # A no-show is recorded as a strike (counted in Phase 2, rule 8).
    # Switching back to present removes it. Never more than one per sign-up.
    if outcome == "no_show":
        cur.execute(
            """INSERT INTO strikes (volunteer_id, signup_id, reason)
               SELECT %s, %s, 'no_show'
               WHERE NOT EXISTS (SELECT 1 FROM strikes WHERE signup_id = %s AND reason = 'no_show')""",
            (signup["volunteer_id"], signup_id, signup_id),
        )
    else:
        cur.execute("DELETE FROM strikes WHERE signup_id = %s AND reason = 'no_show'", (signup_id,))
    return shift["id"]


def close_shift(cur, shift_id):
    """Coordinator: mark the shift completed once every confirmed volunteer is marked."""
    shift = lock_shift(cur, shift_id)
    problem = check_can_mark(cur, shift)
    if problem:
        raise AttendanceRefused(problem)
    cur.execute("SELECT count(*) AS n FROM signups WHERE shift_id = %s AND status = 'confirmed'",
                (shift_id,))
    unmarked = cur.fetchone()["n"]
    if unmarked:
        raise AttendanceRefused(
            f"{unmarked} volunteer{' still needs' if unmarked == 1 else 's still need'} to be marked "
            "before the shift can be closed.")
    cur.execute("UPDATE shifts SET status = 'completed' WHERE id = %s", (shift_id,))

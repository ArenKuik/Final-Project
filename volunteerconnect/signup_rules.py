"""Sign-up business rules (PRD, "Business rules").

Every function here takes a cursor and runs inside the caller's
transaction (see db.transaction()). The routes in signups.py only open the
transaction and call these.

Locking, so that two requests at the same moment can't both succeed:
  - The shift row is locked first (SELECT ... FOR UPDATE). Everything that
    changes a shift's sign-ups (sign up, cancel, promote) takes this lock,
    so they run one at a time per shift.
  - Then the volunteer's user row is locked. That stops one volunteer
    signing up for two overlapping shifts at once from two browser tabs.
  - Always shift first, then volunteer, so two transactions can never wait
    on each other in a circle (deadlock).

Phase 2 adds rule 4 (travel buffer) and rule 8 (strike block) as more
checks inside eligibility_errors().
"""
from werkzeug.exceptions import NotFound

from .volunteer_status import volunteer_statuses

LATE_CANCEL_HOURS = 24          # rule 7 (placeholder to confirm with the coordinator)
ACTIVE_SIGNUP = ("confirmed", "waitlisted")
# Statuses that take up one of the shift's spots.
TAKES_A_SPOT = ("confirmed", "present", "no_show")


class SignupRefused(Exception):
    """A rule said no. The message is shown to the volunteer."""


# ---------- loading and locking ----------

def lock_shift(cur, shift_id):
    """Lock the shift row until the transaction ends. 404 if it doesn't exist."""
    cur.execute(
        """SELECT sh.*, s.name AS school_name,
                  sh.start_at <= now() AS started,
                  sh.start_at - now() < make_interval(hours => %s) AS starts_soon
           FROM shifts sh JOIN schools s ON s.id = sh.school_id
           WHERE sh.id = %s
           FOR UPDATE OF sh""",
        (LATE_CANCEL_HOURS, shift_id),
    )
    shift = cur.fetchone()
    if shift is None:
        raise NotFound()
    return shift


def lock_volunteer(cur, volunteer_id):
    cur.execute("SELECT id, email, role, status FROM users WHERE id = %s FOR UPDATE", (volunteer_id,))
    return cur.fetchone()


def expire_waitlists(cur):
    """Rule 12: once a shift has started, its waitlist is over."""
    cur.execute(
        """UPDATE signups SET status = 'cancelled'
           WHERE status = 'waitlisted'
             AND shift_id IN (SELECT id FROM shifts WHERE start_at <= now())"""
    )


# ---------- the checks (each returns an error message, or None) ----------

def check_shift_open(shift):
    if shift["status"] not in ("open", "full"):
        return f"This shift is {shift['status']} and isn't taking sign-ups."
    if shift["started"]:
        return "This shift has already started."
    return None


def check_clearance(cur, volunteer, shift):
    """Rule 1: every clearance the school requires, unexpired on the shift date."""
    # Status is derived from unexpired clearances, never read from the stored
    # users.status column, which goes stale when a clearance expires.
    if volunteer_statuses([volunteer["id"]]).get(volunteer["id"]) != "active":
        return "Your account is pending until the coordinator records your clearances."
    cur.execute(
        """SELECT req.type
           FROM shifts sh
           JOIN schools s ON s.id = sh.school_id,
                unnest(s.required_clearances) AS req(type)
           WHERE sh.id = %s
             AND NOT EXISTS (
                 SELECT 1 FROM clearances c
                 WHERE c.volunteer_id = %s AND c.type = req.type
                   AND c.expires_on >= (sh.start_at AT TIME ZONE 'America/Los_Angeles')::date)
           ORDER BY req.type""",
        (shift["id"], volunteer["id"]),
    )
    missing = [row["type"].replace("_", " ") for row in cur.fetchall()]
    if missing:
        return (f"{shift['school_name']} requires a {' and '.join(missing)} clearance "
                "that is valid on the shift date.")
    return None


def check_overlap(cur, volunteer_id, shift):
    """Rule 3: no other confirmed or waitlisted sign-up whose times overlap.
    Back-to-back is fine (one ends at 11:00, the next starts at 11:00)."""
    cur.execute(
        """SELECT s.name AS school_name,
                  to_char(o.start_at AT TIME ZONE 'America/Los_Angeles', 'Mon DD, HH24:MI') AS starts,
                  to_char(o.end_at AT TIME ZONE 'America/Los_Angeles', 'HH24:MI') AS ends
           FROM signups su
           JOIN shifts o ON o.id = su.shift_id
           JOIN schools s ON s.id = o.school_id
           WHERE su.volunteer_id = %s
             AND su.status IN ('confirmed', 'waitlisted')
             AND o.status <> 'cancelled'
             AND o.id <> %s
             AND o.start_at < %s AND %s < o.end_at
           LIMIT 1""",
        (volunteer_id, shift["id"], shift["end_at"], shift["start_at"]),
    )
    clash = cur.fetchone()
    if clash:
        return (f"This overlaps your shift at {clash['school_name']} "
                f"({clash['starts']}–{clash['ends']}).")
    return None


def eligibility_errors(cur, volunteer, shift):
    """All the checks a volunteer must pass to hold a place on this shift.
    Used for both signing up and waitlist promotion."""
    errors = [
        check_shift_open(shift),
        check_clearance(cur, volunteer, shift),
        check_overlap(cur, volunteer["id"], shift),
    ]
    return [e for e in errors if e]


# ---------- counting and shift status ----------

def spots_taken(cur, shift_id):
    cur.execute(
        "SELECT count(*) AS n FROM signups WHERE shift_id = %s "
        "AND status IN ('confirmed', 'present', 'no_show')",
        (shift_id,),
    )
    return cur.fetchone()["n"]


def sync_shift_status(cur, shift_id):
    """Mark the shift full or open to match its confirmed sign-ups."""
    cur.execute(
        """UPDATE shifts SET status = CASE
               WHEN (SELECT count(*) FROM signups
                     WHERE shift_id = %s AND status IN ('confirmed', 'present', 'no_show')) >= capacity
               THEN 'full' ELSE 'open' END
           WHERE id = %s AND status IN ('open', 'full')""",
        (shift_id, shift_id),
    )


def waitlist_position(cur, signup_id):
    """1 = next in line."""
    cur.execute(
        """SELECT count(*) AS n FROM signups w, signups me
           WHERE me.id = %s AND w.shift_id = me.shift_id AND w.status = 'waitlisted'
             AND (w.created_at, w.id) <= (me.created_at, me.id)""",
        (signup_id,),
    )
    return cur.fetchone()["n"]


# ---------- actions ----------

def sign_up(cur, volunteer_id, shift_id):
    """Sign a volunteer up. Returns (signup_id, "confirmed" or "waitlisted").
    Raises SignupRefused with a message if a rule says no."""
    shift = lock_shift(cur, shift_id)
    volunteer = lock_volunteer(cur, volunteer_id)

    cur.execute("SELECT * FROM signups WHERE shift_id = %s AND volunteer_id = %s",
                (shift_id, volunteer_id))
    existing = cur.fetchone()
    if existing and existing["status"] in ACTIVE_SIGNUP:
        raise SignupRefused(f"You're already {existing['status']} for this shift.")
    if existing and existing["status"] in ("present", "no_show"):
        raise SignupRefused("Attendance has already been recorded for this shift.")

    errors = eligibility_errors(cur, volunteer, shift)
    if errors:
        raise SignupRefused(" ".join(errors))

    # Rule 2: a free spot means confirmed; otherwise join the waitlist.
    status = "confirmed" if spots_taken(cur, shift_id) < shift["capacity"] else "waitlisted"

    if existing:
        # Rule 11: rejoining reuses the same row (shift_id + volunteer_id is
        # UNIQUE) and resets created_at, so they go to the back of the line.
        cur.execute(
            "UPDATE signups SET status = %s, created_at = now() WHERE id = %s RETURNING id",
            (status, existing["id"]),
        )
    else:
        cur.execute(
            "INSERT INTO signups (shift_id, volunteer_id, status) VALUES (%s, %s, %s) RETURNING id",
            (shift_id, volunteer_id, status),
        )
    signup_id = cur.fetchone()["id"]
    sync_shift_status(cur, shift_id)
    return signup_id, status


def cancel_signup(cur, signup_id, by_coordinator=False):
    """Cancel a sign-up, record a late cancel (rule 7), and promote from the
    waitlist if a confirmed spot opened (rule 5).

    Returns {"late": bool, "promoted": email or None}.
    The caller checks that a volunteer only cancels their own sign-up."""
    cur.execute("SELECT shift_id FROM signups WHERE id = %s", (signup_id,))
    row = cur.fetchone()
    if row is None:
        raise NotFound()
    shift = lock_shift(cur, row["shift_id"])
    cur.execute("SELECT * FROM signups WHERE id = %s FOR UPDATE", (signup_id,))
    signup = cur.fetchone()

    if signup["status"] not in ACTIVE_SIGNUP:
        raise SignupRefused(f"This sign-up is already {signup['status'].replace('_', ' ')}.")
    if shift["started"] and not by_coordinator:
        raise SignupRefused("This shift has already started, so it can't be cancelled here. "
                            "Please contact the coordinator.")

    cur.execute("UPDATE signups SET status = 'cancelled' WHERE id = %s", (signup_id,))

    # Rule 7: a volunteer cancelling a confirmed place within 24 hours of the
    # start is a late cancel. Recorded as a strike for Phase 2 (rule 8).
    late = (signup["status"] == "confirmed" and not by_coordinator
            and shift["starts_soon"] and not shift["started"])
    if late:
        cur.execute(
            "INSERT INTO strikes (volunteer_id, signup_id, reason) VALUES (%s, %s, 'late_cancel')",
            (signup["volunteer_id"], signup_id),
        )

    promoted = None
    if signup["status"] == "confirmed":
        promoted = promote_from_waitlist(cur, shift)
    sync_shift_status(cur, shift["id"])
    return {"late": late, "promoted": promoted}


def promote_from_waitlist(cur, shift):
    """Rule 5: give the free spot to the earliest waitlisted volunteer who
    still passes the rules. Anyone skipped stays waitlisted with the same
    created_at, so they keep their place. Returns the promoted email or None.
    Must be called with the shift already locked."""
    if spots_taken(cur, shift["id"]) >= shift["capacity"]:
        return None
    cur.execute(
        "SELECT id, volunteer_id FROM signups WHERE shift_id = %s AND status = 'waitlisted' "
        "ORDER BY created_at, id",
        (shift["id"],),
    )
    for candidate in cur.fetchall():
        volunteer = lock_volunteer(cur, candidate["volunteer_id"])
        if not eligibility_errors(cur, volunteer, shift):
            cur.execute("UPDATE signups SET status = 'confirmed' WHERE id = %s", (candidate["id"],))
            return volunteer["email"]
    return None  # nobody eligible: the spot stays open


def cancel_shift_signups(cur, shift_id):
    """When the coordinator cancels a shift, its sign-ups are cancelled too."""
    cur.execute(
        "UPDATE signups SET status = 'cancelled' "
        "WHERE shift_id = %s AND status IN ('confirmed', 'waitlisted')",
        (shift_id,),
    )

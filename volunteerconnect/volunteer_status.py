"""A volunteer's status (pending / active) is derived, never trusted from storage.

A volunteer is 'active' if they hold at least one clearance that has not
expired (expires_on >= today in Los Angeles), and 'pending' otherwise.

Because it is calculated when it is needed, a volunteer becomes pending on
the day their last clearance expires, without anyone recording or revoking
anything. The users.status column is NOT authoritative for volunteers.
"""
from .db import query

# Used by volunteer_statuses() below, and by the tests.
VOLUNTEER_STATUS_SQL = """
    SELECT u.id,
           CASE WHEN EXISTS (
                    SELECT 1 FROM clearances c
                    WHERE c.volunteer_id = u.id
                      AND c.expires_on >= (now() AT TIME ZONE 'America/Los_Angeles')::date)
                THEN 'active' ELSE 'pending' END AS status
    FROM users u
    WHERE u.id = ANY(%s) AND u.role = 'volunteer'
"""


def volunteer_statuses(volunteer_ids):
    """Return {volunteer_id: 'active' or 'pending'} for the given ids, in one query."""
    if not volunteer_ids:
        return {}
    rows = query(VOLUNTEER_STATUS_SQL, (list(volunteer_ids),))
    return {row["id"]: row["status"] for row in rows}

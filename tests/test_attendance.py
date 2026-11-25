"""Attendance: PRD rules 9 (hours from 'present') and 10 (only after the start)."""
from datetime import timedelta

import pytest

from tests.helpers import (coordinator_id, counts, login_as, make_school, make_shift, make_signup,
                           make_user, one, run_sql)
from volunteerconnect.attendance_rules import HOURS_BY_VOLUNTEER_SQL

pytestmark = pytest.mark.usefixtures("cleanup_test_data")

STARTED = timedelta(minutes=-30)


# ---------- helpers ----------

@pytest.fixture
def school():
    return make_school()


@pytest.fixture
def teacher(school):
    return make_user("teacher", school_id=school["id"])


def ended_shift(school, **kwargs):
    """A 2-hour shift that started 3 hours ago, so it ended an hour ago."""
    return make_shift(school["id"], start_in=timedelta(hours=-3), hours=2, **kwargs)


def started_shift(school, **kwargs):
    """A shift that began 30 minutes ago and runs 2 hours."""
    return make_shift(school["id"], start_in=STARTED, hours=2, **kwargs)


def volunteer_signup(shift, status="confirmed"):
    volunteer = make_user("volunteer", status="active")
    return volunteer, make_signup(shift["id"], volunteer["id"], status=status)


def mark(client, signup, outcome):
    return client.post(f"/signups/{signup['id']}/attendance", data={"outcome": outcome})


def status_of(signup):
    return one("SELECT status FROM signups WHERE id = %s", (signup["id"],))["status"]


def no_show_strikes(signup):
    return run_sql("SELECT * FROM strikes WHERE signup_id = %s AND reason = 'no_show'", (signup["id"],))


def shift_status(shift):
    return one("SELECT status FROM shifts WHERE id = %s", (shift["id"],))["status"]


# ---------- rule 10: only after the start ----------

def test_marking_before_shift_starts_is_refused(client, school, teacher):
    shift = make_shift(school["id"], start_in=timedelta(days=2))
    _, signup = volunteer_signup(shift)
    login_as(client, teacher["id"])
    resp = mark(client, signup, "present")
    assert resp.status_code == 400
    assert b"once the shift has started" in resp.data
    assert status_of(signup) == "confirmed"


def test_no_mark_buttons_before_start(client, school, teacher):
    shift = make_shift(school["id"], start_in=timedelta(days=2))
    volunteer_signup(shift)
    login_as(client, teacher["id"])
    page = client.get(f"/shifts/{shift['id']}").data
    assert b"/attendance" not in page
    assert b"Attendance can be marked once the shift starts" in page


def test_mark_buttons_after_start(client, school, teacher):
    shift = started_shift(school)
    volunteer_signup(shift)
    login_as(client, teacher["id"])
    assert b"/attendance" in client.get(f"/shifts/{shift['id']}").data


# ---------- permissions ----------

def test_teacher_at_other_school_cannot_mark(client, school):
    shift = started_shift(school)
    _, signup = volunteer_signup(shift)
    other_school = make_school()
    login_as(client, make_user("teacher", school_id=other_school["id"])["id"])
    before = counts()
    assert mark(client, signup, "present").status_code == 403
    assert counts() == before


def test_volunteer_cannot_mark_or_close(client, school):
    shift = started_shift(school)
    volunteer, signup = volunteer_signup(shift)
    before = counts()
    # Not even their own sign-up.
    login_as(client, volunteer["id"])
    assert mark(client, signup, "present").status_code == 403
    assert client.post(f"/shifts/{shift['id']}/complete").status_code == 403
    assert counts() == before


def test_teacher_cannot_close_shift(client, school, teacher):
    shift = started_shift(school)
    _, signup = volunteer_signup(shift, status="present")
    login_as(client, teacher["id"])
    assert client.post(f"/shifts/{shift['id']}/complete").status_code == 403
    assert shift_status(shift) == "open"


def test_coordinator_can_mark(client, school):
    shift = started_shift(school)
    _, signup = volunteer_signup(shift)
    login_as(client, coordinator_id())
    assert mark(client, signup, "present").status_code == 302
    assert status_of(signup) == "present"


# ---------- outcomes and strikes ----------

def test_present_saves(client, school, teacher):
    shift = started_shift(school)
    _, signup = volunteer_signup(shift)
    login_as(client, teacher["id"])
    assert mark(client, signup, "present").status_code == 302
    assert status_of(signup) == "present"
    assert no_show_strikes(signup) == []


def test_no_show_creates_strike_and_present_removes_it(client, school, teacher):
    shift = started_shift(school)
    volunteer, signup = volunteer_signup(shift)
    login_as(client, teacher["id"])

    assert mark(client, signup, "no_show").status_code == 302
    assert status_of(signup) == "no_show"
    (strike,) = no_show_strikes(signup)
    assert strike["volunteer_id"] == volunteer["id"]

    assert mark(client, signup, "present").status_code == 302
    assert status_of(signup) == "present"
    assert no_show_strikes(signup) == []


def test_no_show_twice_leaves_one_strike(client, school, teacher):
    shift = started_shift(school)
    _, signup = volunteer_signup(shift)
    login_as(client, teacher["id"])
    mark(client, signup, "no_show")
    mark(client, signup, "no_show")
    assert len(no_show_strikes(signup)) == 1


def test_invalid_outcome_is_400(client, school, teacher):
    shift = started_shift(school)
    _, signup = volunteer_signup(shift)
    login_as(client, teacher["id"])
    assert mark(client, signup, "cancelled").status_code == 400
    assert client.post(f"/signups/{signup['id']}/attendance", data={}).status_code == 400
    assert status_of(signup) == "confirmed"


@pytest.mark.parametrize("status", ["waitlisted", "cancelled"])
def test_cannot_mark_waitlisted_or_cancelled(client, school, teacher, status):
    shift = started_shift(school)
    _, signup = volunteer_signup(shift, status=status)
    login_as(client, teacher["id"])
    resp = mark(client, signup, "present")
    assert resp.status_code == 400
    assert b"Only confirmed volunteers can be marked" in resp.data
    assert status_of(signup) == status


def test_cannot_mark_on_cancelled_shift(client, school, teacher):
    shift = started_shift(school, status="cancelled")
    _, signup = volunteer_signup(shift)
    login_as(client, teacher["id"])
    resp = mark(client, signup, "present")
    assert resp.status_code == 400
    assert status_of(signup) == "confirmed"


def test_missing_signup_is_404(client, teacher):
    login_as(client, teacher["id"])
    assert client.post("/signups/999999999/attendance", data={"outcome": "present"}).status_code == 404


# ---------- closing and reopening the shift ----------

def close(client, shift):
    return client.post(f"/shifts/{shift['id']}/complete")


def reopen(client, shift):
    return client.post(f"/shifts/{shift['id']}/reopen")


def test_close_before_end_time_refused(client, school):
    """Started but not yet ended, everyone marked: still too early to close."""
    shift = started_shift(school)  # started 30 min ago, ends in 90 min
    volunteer_signup(shift, status="present")
    login_as(client, coordinator_id())
    resp = close(client, shift)
    assert resp.status_code == 400
    assert b"can be closed only after it has ended" in resp.data
    assert shift_status(shift) == "open"
    page = client.get(f"/shifts/{shift['id']}").data
    assert b"Close shift" not in page and b"can be closed after it ends" in page


def test_close_before_start_refused(client, school):
    shift = make_shift(school["id"], start_in=timedelta(days=2))
    login_as(client, coordinator_id())
    assert close(client, shift).status_code == 400
    assert shift_status(shift) == "open"


def test_close_refused_while_someone_is_unmarked(client, school):
    shift = ended_shift(school)
    volunteer_signup(shift, status="present")
    volunteer_signup(shift)  # still confirmed
    login_as(client, coordinator_id())
    resp = close(client, shift)
    assert resp.status_code == 400
    assert b"1 volunteer still needs to be marked" in resp.data
    assert shift_status(shift) == "open"


def test_close_after_end_time_with_all_marked(client, school, teacher):
    shift = ended_shift(school)
    _, a = volunteer_signup(shift)
    _, b = volunteer_signup(shift)
    login_as(client, teacher["id"])
    mark(client, a, "present")
    mark(client, b, "no_show")

    login_as(client, coordinator_id())
    assert b"Close shift" in client.get(f"/shifts/{shift['id']}").data
    assert close(client, shift).status_code == 302
    assert shift_status(shift) == "completed"


def test_marks_locked_after_close(client, school, teacher):
    shift = ended_shift(school)
    _, signup = volunteer_signup(shift, status="no_show")
    login_as(client, coordinator_id())
    close(client, shift)
    assert shift_status(shift) == "completed"

    login_as(client, teacher["id"])
    resp = mark(client, signup, "present")
    assert resp.status_code == 400
    assert b"closed" in resp.data
    assert status_of(signup) == "no_show"


def test_coordinator_reopens_and_marks_can_change(client, school, teacher):
    shift = ended_shift(school, capacity=2)
    _, a = volunteer_signup(shift, status="no_show")
    _, b = volunteer_signup(shift, status="present")
    login_as(client, coordinator_id())
    close(client, shift)
    assert shift_status(shift) == "completed"
    assert b"Reopen shift" in client.get(f"/shifts/{shift['id']}").data

    assert reopen(client, shift).status_code == 302
    assert shift_status(shift) == "full"  # 2 of 2 spots taken (present + no_show)

    # The teacher can correct the mistake, which also removes the strike.
    run_sql("INSERT INTO strikes (volunteer_id, signup_id, reason) VALUES (%s, %s, 'no_show')",
            (a["volunteer_id"], a["id"]))
    login_as(client, teacher["id"])
    assert mark(client, a, "present").status_code == 302
    assert status_of(a) == "present"
    assert no_show_strikes(a) == []

    # And the coordinator can close it again.
    login_as(client, coordinator_id())
    assert close(client, shift).status_code == 302
    assert shift_status(shift) == "completed"


def test_teacher_and_volunteer_cannot_reopen(client, school, teacher):
    shift = ended_shift(school, status="completed")
    volunteer, _ = volunteer_signup(shift, status="present")
    for user in (teacher, volunteer):
        login_as(client, user["id"])
        assert reopen(client, shift).status_code == 403
    assert shift_status(shift) == "completed"


def test_reopen_only_closed_shifts(client, school):
    shift = ended_shift(school)
    login_as(client, coordinator_id())
    resp = reopen(client, shift)
    assert resp.status_code == 400
    assert b"Only a closed shift can be reopened" in resp.data
    assert shift_status(shift) == "open"


# ---------- rule 9: hours come from 'present' ----------

def test_hours_count_only_present(client, school, teacher):
    shift = started_shift(school)  # 2 hours long
    present_vol, present = volunteer_signup(shift)
    absent_vol, absent = volunteer_signup(shift)
    login_as(client, teacher["id"])
    mark(client, present, "present")
    mark(client, absent, "no_show")

    rows = run_sql(HOURS_BY_VOLUNTEER_SQL, ([present_vol["id"], absent_vol["id"]],))
    hours = {row["volunteer_id"]: float(row["hours"]) for row in rows}
    assert hours == {present_vol["id"]: 2.0}  # the no-show has no hours

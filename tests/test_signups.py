"""Volunteer sign-ups: PRD business rules 1, 2, 3, 5, 7, 11 and 12."""
import threading
import time
from datetime import timedelta

import psycopg
import pytest

from tests.helpers import (assert_forbidden_everywhere, coordinator_id, counts, future, login_as,
                           make_clearance, make_school, make_shift, make_signup, make_user, one,
                           run_sql)

pytestmark = pytest.mark.usefixtures("cleanup_test_data")


# ---------- helpers ----------

@pytest.fixture
def school():
    """A school that requires a TB test."""
    return make_school(required=["tb_test"])


def cleared_volunteer(expires_on=None):
    """An active volunteer with a TB clearance (valid for a year by default)."""
    volunteer = make_user("volunteer", status="active")
    make_clearance(volunteer["id"], "tb_test", expires_on=expires_on)
    return volunteer


def new_client(app):
    client = app.test_client()
    client.environ_base["wsgi.url_scheme"] = "https"
    return client


def sign_up(client, volunteer, shift):
    login_as(client, volunteer["id"])
    return client.post(f"/shifts/{shift['id']}/signup")


def signup_of(volunteer, shift):
    return one("SELECT * FROM signups WHERE volunteer_id = %s AND shift_id = %s",
               (volunteer["id"], shift["id"]))


def status_of(volunteer, shift):
    row = signup_of(volunteer, shift)
    return row["status"] if row else None


def shift_status(shift):
    return one("SELECT status FROM shifts WHERE id = %s", (shift["id"],))["status"]


def cancel(client, volunteer, signup):
    login_as(client, volunteer["id"])
    return client.post(f"/signups/{signup['id']}/cancel")


def strikes_for(volunteer):
    return run_sql("SELECT * FROM strikes WHERE volunteer_id = %s", (volunteer["id"],))


# ---------- what volunteers can see ----------

def test_cleared_volunteer_sees_shift_with_spots_left(client, school):
    shift = make_shift(school["id"], capacity=3)
    login_as(client, cleared_volunteer()["id"])
    page = client.get("/shifts")
    assert page.status_code == 200
    assert f"/shifts/{shift['id']}/signup".encode() in page.data
    assert b"3 of 3 left" in page.data


def test_uncleared_volunteer_does_not_see_shift(client, school):
    shift = make_shift(school["id"])
    volunteer = make_user("volunteer", status="active")
    make_clearance(volunteer["id"], "background_check")  # wrong type
    login_as(client, volunteer["id"])
    assert f"/shifts/{shift['id']}/signup".encode() not in client.get("/shifts").data


def test_pending_volunteer_sees_no_shifts(client):
    open_school = make_school(required=[])  # requires nothing
    shift = make_shift(open_school["id"])
    login_as(client, make_user("volunteer")["id"])  # pending
    page = client.get("/shifts")
    assert page.status_code == 200
    assert f"/shifts/{shift['id']}/signup".encode() not in page.data
    assert b"pending" in page.data


def test_full_shift_shown_as_full(client, school):
    shift = make_shift(school["id"], capacity=1)
    make_signup(shift["id"], cleared_volunteer()["id"])
    run_sql("UPDATE shifts SET status = 'full' WHERE id = %s", (shift["id"],))
    login_as(client, cleared_volunteer()["id"])
    page = client.get("/shifts").data
    assert b"Full" in page and b"Join waitlist" in page


def test_signed_up_shift_still_listed_after_clearance_revoked(client, school):
    volunteer = cleared_volunteer()
    shift = make_shift(school["id"])
    assert sign_up(client, volunteer, shift).status_code == 302
    run_sql("DELETE FROM clearances WHERE volunteer_id = %s", (volunteer["id"],))
    page = client.get("/shifts").data
    assert b"Signed up" in page


# ---------- rule 1: clearance ----------

def test_not_cleared_refused(client, school):
    volunteer = make_user("volunteer", status="active")
    make_clearance(volunteer["id"], "background_check")
    shift = make_shift(school["id"])
    resp = sign_up(client, volunteer, shift)
    assert resp.status_code == 400
    assert b"requires a tb test clearance" in resp.data
    assert signup_of(volunteer, shift) is None


def test_clearance_expiring_before_shift_date_refused(client, school):
    shift = make_shift(school["id"])  # 30 days ahead
    volunteer = cleared_volunteer(expires_on=future(10))
    resp = sign_up(client, volunteer, shift)
    assert resp.status_code == 400
    assert signup_of(volunteer, shift) is None


def test_pending_volunteer_refused(client):
    open_school = make_school(required=[])
    shift = make_shift(open_school["id"])
    volunteer = make_user("volunteer")  # pending
    resp = sign_up(client, volunteer, shift)
    assert resp.status_code == 400
    assert b"pending" in resp.data
    assert signup_of(volunteer, shift) is None


def test_volunteer_whose_only_clearance_expired_yesterday_is_pending(client):
    """Status is derived from unexpired clearances, not the stored column.
    The school requires nothing, so only the derived status can block them."""
    open_school = make_school(required=[])
    shift = make_shift(open_school["id"])
    volunteer = cleared_volunteer()  # stored users.status is 'active'
    run_sql("UPDATE clearances SET expires_on = (now() AT TIME ZONE 'America/Los_Angeles')::date - 1 "
            "WHERE volunteer_id = %s", (volunteer["id"],))
    assert one("SELECT status FROM users WHERE id = %s", (volunteer["id"],))["status"] == "active"

    login_as(client, volunteer["id"])
    page = client.get("/shifts")
    assert f"/shifts/{shift['id']}/signup".encode() not in page.data
    assert b"pending" in page.data

    resp = sign_up(client, volunteer, shift)
    assert resp.status_code == 400
    assert b"pending" in resp.data
    assert signup_of(volunteer, shift) is None


# ---------- rule 2: capacity and waitlist ----------

def test_third_volunteer_on_two_person_shift_is_waitlisted(client, school):
    shift = make_shift(school["id"], capacity=2)
    a, b, c = cleared_volunteer(), cleared_volunteer(), cleared_volunteer()
    for v in (a, b):
        assert sign_up(client, v, shift).status_code == 302
    assert shift_status(shift) == "full"
    resp = sign_up(client, c, shift)
    assert resp.status_code == 302
    assert [status_of(v, shift) for v in (a, b, c)] == ["confirmed", "confirmed", "waitlisted"]


def test_cannot_sign_up_twice(client, school):
    shift = make_shift(school["id"])
    volunteer = cleared_volunteer()
    sign_up(client, volunteer, shift)
    resp = sign_up(client, volunteer, shift)
    assert resp.status_code == 400
    assert b"already confirmed" in resp.data


# ---------- rule 3: overlap ----------

def test_overlapping_shifts_second_refused(client, school):
    other_school = make_school(required=["tb_test"])
    first = make_shift(school["id"], start_in=timedelta(days=5), hours=2)
    second = make_shift(other_school["id"], start_in=timedelta(days=5, hours=1), hours=2)
    volunteer = cleared_volunteer()
    assert sign_up(client, volunteer, first).status_code == 302
    resp = sign_up(client, volunteer, second)
    assert resp.status_code == 400
    assert b"overlaps your shift" in resp.data
    assert signup_of(volunteer, second) is None


def test_overlap_with_waitlisted_signup_also_refused(client, school):
    first = make_shift(school["id"], start_in=timedelta(days=5), capacity=1)
    make_signup(first["id"], cleared_volunteer()["id"])  # fills it
    second = make_shift(school["id"], start_in=timedelta(days=5, hours=1))
    volunteer = cleared_volunteer()
    sign_up(client, volunteer, first)
    assert status_of(volunteer, first) == "waitlisted"
    assert sign_up(client, volunteer, second).status_code == 400


def test_back_to_back_shifts_allowed(client, school):
    first = make_shift(school["id"], start_in=timedelta(days=5), hours=2)
    second = make_shift(school["id"], start_in=timedelta(days=5, hours=2), hours=2)
    volunteer = cleared_volunteer()
    assert sign_up(client, volunteer, first).status_code == 302
    assert sign_up(client, volunteer, second).status_code == 302
    assert status_of(volunteer, second) == "confirmed"


# ---------- rule 5: waitlist promotion ----------

def test_cancel_promotes_first_waitlisted(client, school):
    shift = make_shift(school["id"], capacity=1)
    a, b, c = cleared_volunteer(), cleared_volunteer(), cleared_volunteer()
    for v in (a, b, c):
        sign_up(client, v, shift)
    assert cancel(client, a, signup_of(a, shift)).status_code == 302
    assert [status_of(v, shift) for v in (a, b, c)] == ["cancelled", "confirmed", "waitlisted"]
    assert shift_status(shift) == "full"


def test_ineligible_first_waitlisted_is_skipped_and_keeps_place(client, school):
    shift = make_shift(school["id"], start_in=timedelta(days=5), capacity=1)
    holder, first, second = cleared_volunteer(), cleared_volunteer(), cleared_volunteer()
    for v in (holder, first, second):
        sign_up(client, v, shift)
    # Since joining the waitlist, `first` got a confirmed place on an
    # overlapping shift (inserted directly, as if by a later change).
    clash = make_shift(school["id"], start_in=timedelta(days=5, minutes=30))
    make_signup(clash["id"], first["id"])
    first_joined = signup_of(first, shift)["created_at"]

    cancel(client, holder, signup_of(holder, shift))
    assert status_of(second, shift) == "confirmed"
    assert status_of(first, shift) == "waitlisted"
    assert signup_of(first, shift)["created_at"] == first_joined  # place kept


def test_promotion_follows_join_order(client, school):
    shift = make_shift(school["id"], capacity=1)
    holder = cleared_volunteer()
    waiters = [cleared_volunteer() for _ in range(3)]
    for v in [holder] + waiters:
        sign_up(client, v, shift)

    current = holder
    for expected in waiters:
        cancel(client, current, signup_of(current, shift))
        assert status_of(expected, shift) == "confirmed"
        current = expected


def test_no_eligible_waitlisted_leaves_spot_open(client, school):
    shift = make_shift(school["id"], capacity=1)
    holder, waiter = cleared_volunteer(), cleared_volunteer()
    sign_up(client, holder, shift)
    sign_up(client, waiter, shift)
    run_sql("DELETE FROM clearances WHERE volunteer_id = %s", (waiter["id"],))  # no longer cleared
    cancel(client, holder, signup_of(holder, shift))
    assert status_of(waiter, shift) == "waitlisted"
    assert shift_status(shift) == "open"


def test_coordinator_remove_promotes_next(client, school):
    shift = make_shift(school["id"], capacity=1)
    holder, waiter = cleared_volunteer(), cleared_volunteer()
    sign_up(client, holder, shift)
    sign_up(client, waiter, shift)
    login_as(client, coordinator_id())
    resp = client.post(f"/signups/{signup_of(holder, shift)['id']}/remove")
    assert resp.status_code == 302
    assert status_of(holder, shift) == "cancelled"
    assert status_of(waiter, shift) == "confirmed"


# ---------- rule 11: rejoin ----------

def test_cancel_then_rejoin_goes_to_the_back(client, school):
    shift = make_shift(school["id"], capacity=1)
    holder, a, b = cleared_volunteer(), cleared_volunteer(), cleared_volunteer()
    for v in (holder, a, b):
        sign_up(client, v, shift)
    original = signup_of(a, shift)

    cancel(client, a, original)
    assert sign_up(client, a, shift).status_code == 302
    rejoined = signup_of(a, shift)
    assert rejoined["id"] == original["id"]               # same row reused
    assert rejoined["created_at"] > original["created_at"]
    assert rejoined["status"] == "waitlisted"

    # b joined after a originally, but is now ahead of a.
    cancel(client, holder, signup_of(holder, shift))
    assert status_of(b, shift) == "confirmed"
    assert status_of(a, shift) == "waitlisted"


# ---------- rule 7: late cancel ----------

def test_late_cancel_recorded_within_24_hours(client, school):
    shift = make_shift(school["id"], start_in=timedelta(hours=10))
    volunteer = cleared_volunteer()
    sign_up(client, volunteer, shift)
    resp = cancel(client, volunteer, signup_of(volunteer, shift))
    assert resp.status_code == 302
    (strike,) = strikes_for(volunteer)
    assert strike["reason"] == "late_cancel"
    assert strike["signup_id"] == signup_of(volunteer, shift)["id"]


def test_cancel_more_than_24_hours_ahead_is_not_late(client, school):
    shift = make_shift(school["id"], start_in=timedelta(hours=30))
    volunteer = cleared_volunteer()
    sign_up(client, volunteer, shift)
    cancel(client, volunteer, signup_of(volunteer, shift))
    assert strikes_for(volunteer) == []


def test_cancelling_waitlist_place_is_not_late(client, school):
    shift = make_shift(school["id"], start_in=timedelta(hours=10), capacity=1)
    holder, waiter = cleared_volunteer(), cleared_volunteer()
    sign_up(client, holder, shift)
    sign_up(client, waiter, shift)
    cancel(client, waiter, signup_of(waiter, shift))
    assert strikes_for(waiter) == []


def test_coordinator_removal_is_not_late_cancel(client, school):
    shift = make_shift(school["id"], start_in=timedelta(hours=10))
    volunteer = cleared_volunteer()
    sign_up(client, volunteer, shift)
    login_as(client, coordinator_id())
    client.post(f"/signups/{signup_of(volunteer, shift)['id']}/remove")
    assert strikes_for(volunteer) == []


# ---------- rule 12 and started shifts ----------

def test_waitlist_expires_when_shift_starts(client, school):
    shift = make_shift(school["id"], capacity=1)
    holder, waiter = cleared_volunteer(), cleared_volunteer()
    sign_up(client, holder, shift)
    sign_up(client, waiter, shift)
    run_sql("UPDATE shifts SET start_at = now() - interval '5 minutes', end_at = now() + interval '1 hour' "
            "WHERE id = %s", (shift["id"],))
    login_as(client, waiter["id"])
    client.get("/shifts")
    assert status_of(waiter, shift) == "cancelled"
    assert status_of(holder, shift) == "confirmed"


def test_cannot_sign_up_for_started_or_cancelled_shift(client, school):
    started = make_shift(school["id"], start_in=timedelta(minutes=-10))
    cancelled = make_shift(school["id"], status="cancelled")
    volunteer = cleared_volunteer()
    resp = sign_up(client, volunteer, started)
    assert resp.status_code == 400 and b"already started" in resp.data
    resp = sign_up(client, volunteer, cancelled)
    assert resp.status_code == 400 and b"isn&#39;t taking sign-ups" in resp.data
    assert signup_of(volunteer, started) is None and signup_of(volunteer, cancelled) is None


def test_volunteer_cannot_cancel_after_shift_started(client, school):
    shift = make_shift(school["id"], start_in=timedelta(minutes=-10))
    volunteer = cleared_volunteer()
    signup = make_signup(shift["id"], volunteer["id"])
    resp = cancel(client, volunteer, signup)
    assert resp.status_code == 400
    assert b"already started" in resp.data
    assert status_of(volunteer, shift) == "confirmed"
    assert strikes_for(volunteer) == []

    # The coordinator can still remove them.
    login_as(client, coordinator_id())
    assert client.post(f"/signups/{signup['id']}/remove").status_code == 302
    assert status_of(volunteer, shift) == "cancelled"


def test_cancelling_shift_cancels_its_signups(client, school):
    shift = make_shift(school["id"], capacity=1)
    holder, waiter = cleared_volunteer(), cleared_volunteer()
    sign_up(client, holder, shift)
    sign_up(client, waiter, shift)
    login_as(client, coordinator_id())
    assert client.post(f"/shifts/{shift['id']}/cancel").status_code == 302
    assert status_of(holder, shift) == "cancelled" and status_of(waiter, shift) == "cancelled"


# ---------- permissions ----------

def test_volunteer_cannot_cancel_someone_elses_signup(client, school):
    shift = make_shift(school["id"])
    owner, other = cleared_volunteer(), cleared_volunteer()
    sign_up(client, owner, shift)
    resp = cancel(client, other, signup_of(owner, shift))
    assert resp.status_code == 403
    assert status_of(owner, shift) == "confirmed"


def test_teacher_cannot_sign_up(client, school):
    shift = make_shift(school["id"])
    teacher = make_user("teacher", school_id=school["id"])
    login_as(client, teacher["id"])
    before = counts()
    assert client.post(f"/shifts/{shift['id']}/signup").status_code == 403
    assert client.get("/shifts").status_code == 403
    assert client.get("/my-signups").status_code == 403
    assert counts() == before


def test_volunteer_cannot_see_shift_page_or_remove(client, school):
    shift = make_shift(school["id"])
    owner = cleared_volunteer()
    signup = make_signup(shift["id"], owner["id"])
    login_as(client, cleared_volunteer()["id"])
    before = counts()
    assert client.get(f"/shifts/{shift['id']}").status_code == 403
    assert client.post(f"/signups/{signup['id']}/remove").status_code == 403
    assert counts() == before


def test_teacher_sees_own_school_signups_read_only(client, school):
    shift = make_shift(school["id"], capacity=1)
    holder, waiter = cleared_volunteer(), cleared_volunteer()
    make_signup(shift["id"], holder["id"])
    make_signup(shift["id"], waiter["id"], status="waitlisted")
    login_as(client, make_user("teacher", school_id=school["id"])["id"])
    page = client.get(f"/shifts/{shift['id']}")
    assert page.status_code == 200
    assert holder["email"].encode() in page.data and waiter["email"].encode() in page.data
    assert b"/remove" not in page.data  # read-only


def test_teacher_at_other_school_gets_403(client, school):
    shift = make_shift(school["id"])
    other_school = make_school()
    login_as(client, make_user("teacher", school_id=other_school["id"])["id"])
    assert client.get(f"/shifts/{shift['id']}").status_code == 403


def test_coordinator_sees_waitlist_in_order(client, school):
    shift = make_shift(school["id"], capacity=1)
    holder = cleared_volunteer()
    waiters = [cleared_volunteer() for _ in range(2)]
    for v in [holder] + waiters:
        sign_up(client, v, shift)
    login_as(client, coordinator_id())
    page = client.get(f"/shifts/{shift['id']}").data
    assert page.index(waiters[0]["email"].encode()) < page.index(waiters[1]["email"].encode())


# ---------- concurrency ----------

@pytest.mark.parametrize("attempt", range(3))
def test_two_signups_for_last_spot_at_once(app, school, attempt):
    shift = make_shift(school["id"], capacity=1)
    volunteers = [cleared_volunteer(), cleared_volunteer()]
    barrier = threading.Barrier(2)
    results = []

    def go(volunteer):
        client = new_client(app)
        login_as(client, volunteer["id"])
        barrier.wait()  # both threads send their request at the same moment
        results.append(client.post(f"/shifts/{shift['id']}/signup").status_code)

    threads = [threading.Thread(target=go, args=(v,)) for v in volunteers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)

    assert results == [302, 302]
    statuses = sorted(status_of(v, shift) for v in volunteers)
    assert statuses == ["confirmed", "waitlisted"]
    confirmed = one("SELECT count(*) AS n FROM signups WHERE shift_id = %s AND status = 'confirmed'",
                    (shift["id"],))["n"]
    assert confirmed == 1  # never over capacity
    assert shift_status(shift) == "full"


def test_signup_waits_for_the_shift_lock(app, school):
    """Proves sign-up really uses SELECT ... FOR UPDATE: while another
    transaction holds the shift's lock, the sign-up waits."""
    import os
    shift = make_shift(school["id"], capacity=1)
    volunteer = cleared_volunteer()
    holder = psycopg.connect(os.environ["DATABASE_URL"])  # not autocommit: keeps the lock
    holder.execute("SELECT id FROM shifts WHERE id = %s FOR UPDATE", (shift["id"],))

    result = {}

    def go():
        client = new_client(app)
        login_as(client, volunteer["id"])
        result["status"] = client.post(f"/shifts/{shift['id']}/signup").status_code

    thread = threading.Thread(target=go)
    thread.start()
    try:
        # Wait until some backend is blocked waiting for a lock (our sign-up).
        deadline = time.time() + 30
        waiting = 0
        while time.time() < deadline and not waiting:
            waiting = one("SELECT count(*) AS n FROM pg_stat_activity "
                          "WHERE wait_event_type = 'Lock' AND datname = current_database()")["n"]
            if not waiting:
                time.sleep(0.5)
        assert waiting, "the sign-up never waited for the shift lock"
        assert thread.is_alive()
        assert signup_of(volunteer, shift) is None
    finally:
        holder.rollback()  # release the lock
        holder.close()
    thread.join(timeout=30)
    assert result["status"] == 302
    assert status_of(volunteer, shift) == "confirmed"


# ---------- route-level 403 sweep ----------

def test_teacher_gets_403_on_every_volunteer_route(app, client, school):
    """Teachers can view /shifts/<id> for their school, but every volunteer
    action is forbidden."""
    shift = make_shift(school["id"])
    signup = make_signup(shift["id"], cleared_volunteer()["id"])
    login_as(client, make_user("teacher", school_id=school["id"])["id"])
    before = counts()
    for method, url in [("GET", "/shifts"), ("GET", "/my-signups"),
                        ("POST", f"/shifts/{shift['id']}/signup"),
                        ("POST", f"/signups/{signup['id']}/cancel"),
                        ("POST", f"/signups/{signup['id']}/remove")]:
        assert client.open(url, method=method).status_code == 403, url
    assert counts() == before

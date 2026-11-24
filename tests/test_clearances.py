"""Coordinator clearance management and volunteer pending/active status."""
import pytest

from tests.helpers import (assert_forbidden_everywhere, coordinator_id, future, login_as,
                           make_school, make_user, one, past, run_sql)

pytestmark = pytest.mark.usefixtures("cleanup_test_data")


@pytest.fixture
def coordinator(client):
    login_as(client, coordinator_id())
    return client


def record(client, volunteer_id, type_="tb_test", expires_on=None):
    expires_on = expires_on or future()
    return client.post(f"/volunteers/{volunteer_id}/clearances",
                       data={"type": type_, "expires_on": expires_on.isoformat()})


def status_of(user_id):
    return one("SELECT status FROM users WHERE id = %s", (user_id,))["status"]


def clearances_of(user_id):
    return run_sql("SELECT * FROM clearances WHERE volunteer_id = %s ORDER BY id", (user_id,))


def test_volunteer_list_shows_status_and_clearances(coordinator):
    volunteer = make_user("volunteer")
    record(coordinator, volunteer["id"], "background_check")
    resp = coordinator.get("/volunteers")
    assert resp.status_code == 200
    assert volunteer["email"].encode() in resp.data
    assert b"Background check" in resp.data
    assert b"Active" in resp.data


def test_recording_clearance_makes_volunteer_active(coordinator):
    volunteer = make_user("volunteer")
    assert status_of(volunteer["id"]) == "pending"

    resp = record(coordinator, volunteer["id"])
    assert resp.status_code == 302
    assert status_of(volunteer["id"]) == "active"

    (row,) = clearances_of(volunteer["id"])
    assert row["type"] == "tb_test"
    assert row["verified_by"] == coordinator_id()
    assert row["verified_at"] is not None


def test_same_type_twice_updates_expiry(coordinator):
    volunteer = make_user("volunteer")
    record(coordinator, volunteer["id"], expires_on=future(100))
    record(coordinator, volunteer["id"], expires_on=future(400))

    rows = clearances_of(volunteer["id"])
    assert len(rows) == 1
    assert rows[0]["expires_on"] == future(400)


def test_revoking_last_clearance_makes_volunteer_pending(coordinator):
    volunteer = make_user("volunteer")
    record(coordinator, volunteer["id"], "tb_test")
    record(coordinator, volunteer["id"], "background_check")
    tb, bg = clearances_of(volunteer["id"])

    coordinator.post(f"/volunteers/{volunteer['id']}/clearances/{tb['id']}/revoke")
    assert status_of(volunteer["id"]) == "active"  # one clearance still left

    resp = coordinator.post(f"/volunteers/{volunteer['id']}/clearances/{bg['id']}/revoke")
    assert resp.status_code == 302
    assert clearances_of(volunteer["id"]) == []
    assert status_of(volunteer["id"]) == "pending"


def test_past_expiry_rejected(coordinator):
    volunteer = make_user("volunteer")
    resp = record(coordinator, volunteer["id"], expires_on=past())
    assert resp.status_code == 400
    assert b"in the past" in resp.data
    assert clearances_of(volunteer["id"]) == []
    assert status_of(volunteer["id"]) == "pending"


def test_unknown_clearance_type_rejected(coordinator):
    volunteer = make_user("volunteer")
    resp = record(coordinator, volunteer["id"], type_="fingerprints")
    assert resp.status_code == 400
    assert clearances_of(volunteer["id"]) == []


def test_missing_expiry_rejected(coordinator):
    volunteer = make_user("volunteer")
    resp = coordinator.post(f"/volunteers/{volunteer['id']}/clearances", data={"type": "tb_test"})
    assert resp.status_code == 400
    assert clearances_of(volunteer["id"]) == []


def test_only_volunteers_can_hold_clearances(coordinator):
    school = make_school()
    teacher = make_user("teacher", school_id=school["id"])
    assert record(coordinator, teacher["id"]).status_code == 404
    assert coordinator.get(f"/volunteers/{teacher['id']}").status_code == 404


def test_cannot_revoke_another_volunteers_clearance_via_url(coordinator):
    a = make_user("volunteer")
    b = make_user("volunteer")
    record(coordinator, a["id"])
    (clearance,) = clearances_of(a["id"])
    resp = coordinator.post(f"/volunteers/{b['id']}/clearances/{clearance['id']}/revoke")
    assert resp.status_code == 404
    assert len(clearances_of(a["id"])) == 1


@pytest.mark.parametrize("role", ["teacher", "volunteer"])
def test_non_coordinators_get_403(app, client, coordinator, role):
    # Set up a real volunteer with a real clearance, so a successful
    # forbidden request would actually change something.
    target = make_user("volunteer")
    record(coordinator, target["id"])
    (clearance,) = clearances_of(target["id"])

    school = make_school()
    user = make_user(role, school_id=school["id"] if role == "teacher" else None)
    login_as(client, user["id"])
    assert_forbidden_everywhere(
        app, client, "volunteers",
        url_args={"volunteer_id": target["id"], "clearance_id": clearance["id"]},
        post_data={"type": "background_check", "expires_on": future().isoformat()},
    )
    assert status_of(target["id"]) == "active"

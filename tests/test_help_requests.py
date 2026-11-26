"""Help requests (teachers) and turning them into shifts (coordinator)."""
from datetime import date, datetime, timezone

import pytest

from tests.helpers import (assert_forbidden_everywhere, coordinator_id, counts, future, login_as,
                           make_request, make_school, make_shift, make_user, one, past, run_sql)

pytestmark = pytest.mark.usefixtures("cleanup_test_data")


# ---------- helpers ----------

def request_form(day=None, start="09:00", end="11:00", needed=2, activity="Reading buddies"):
    return {"activity": activity, "room": "Room 4", "date": (day or future(30)).isoformat(),
            "start_time": start, "end_time": end, "volunteers_needed": str(needed)}


def approve_data(*rows):
    """Form data for the approve form: rows of (date, start, end, capacity)."""
    data = {}
    for i, (day, start, end, capacity) in enumerate(rows):
        data[f"shifts-{i}-date"] = day.isoformat()
        data[f"shifts-{i}-start_time"] = start
        data[f"shifts-{i}-end_time"] = end
        data[f"shifts-{i}-capacity"] = str(capacity)
    return data


def get_request(request_id):
    return one("SELECT * FROM help_requests WHERE id = %s", (request_id,))


def shifts_of(request_id):
    return run_sql("SELECT * FROM shifts WHERE request_id = %s ORDER BY start_at", (request_id,))


@pytest.fixture
def school():
    return make_school()


@pytest.fixture
def teacher(school):
    return make_user("teacher", school_id=school["id"])


# ---------- the full loop ----------

def test_full_loop(app, client, school, teacher):
    # 1. Teacher submits.
    login_as(client, teacher["id"])
    day = future(20)
    resp = client.post("/requests/new", data=request_form(day=day, start="09:00", end="10:00", needed=3))
    assert resp.status_code == 302
    request_id = int(resp.headers["Location"].rstrip("/").split("/")[-1])
    req = get_request(request_id)
    assert req["status"] == "submitted"
    assert req["school_id"] == school["id"] and req["teacher_id"] == teacher["id"]

    # 2. Coordinator sends it back with a note.
    login_as(client, coordinator_id())
    resp = client.post(f"/requests/{request_id}/send-back", data={"note": "Please start at 10:00 instead."})
    assert resp.status_code == 302
    assert get_request(request_id)["status"] == "changes_requested"

    # 3. Teacher sees the note in the list and on the edit page, edits and resubmits.
    login_as(client, teacher["id"])
    assert b"Please start at 10:00 instead." in client.get("/requests").data
    edit_page = client.get(f"/requests/{request_id}/edit")
    assert edit_page.status_code == 200 and b"Please start at 10:00 instead." in edit_page.data
    resp = client.post(f"/requests/{request_id}/edit",
                       data=request_form(day=day, start="10:00", end="12:00", needed=4))
    assert resp.status_code == 302
    req = get_request(request_id)
    assert req["status"] == "submitted"
    assert str(req["start_time"]) == "10:00:00" and req["volunteers_needed"] == 4

    # 4. Coordinator approves with the default single shift.
    login_as(client, coordinator_id())
    resp = client.post(f"/requests/{request_id}/approve", data=approve_data((day, "10:00", "12:00", 4)))
    assert resp.status_code == 302
    assert get_request(request_id)["status"] == "approved"

    # 5. The shift exists with the right times, capacity and status.
    (shift,) = shifts_of(request_id)
    assert shift["capacity"] == 4
    assert shift["status"] == "open"
    assert shift["school_id"] == school["id"]
    local = one("SELECT start_at AT TIME ZONE 'America/Los_Angeles' AS s, "
                "end_at AT TIME ZONE 'America/Los_Angeles' AS e FROM shifts WHERE id = %s", (shift["id"],))
    assert local["s"] == datetime.combine(day, datetime.strptime("10:00", "%H:%M").time())
    assert local["e"] == datetime.combine(day, datetime.strptime("12:00", "%H:%M").time())


def test_approve_form_prefilled_from_request(client, school, teacher):
    req = make_request(school["id"], teacher["id"], needed=5)
    login_as(client, coordinator_id())
    page = client.get(f"/requests/{req['id']}")
    assert page.status_code == 200
    assert b'name="shifts-0-capacity"' in page.data and b'value="5"' in page.data


def test_approve_into_two_shifts(client, school, teacher):
    req = make_request(school["id"], teacher["id"], start="09:00", end="13:00", needed=4)
    login_as(client, coordinator_id())
    day = req["date"]
    resp = client.post(f"/requests/{req['id']}/approve",
                       data=approve_data((day, "09:00", "11:00", 2), (day, "11:00", "13:00", 3)))
    assert resp.status_code == 302
    shifts = shifts_of(req["id"])
    assert [s["capacity"] for s in shifts] == [2, 3]


def test_times_stored_as_timestamptz_in_los_angeles(client, school, teacher):
    """09:00 in LA is 17:00 UTC in winter (PST, UTC-8) and 16:00 UTC in summer (PDT, UTC-7)."""
    year = date.today().year + 1
    winter, summer = date(year, 1, 15), date(year, 7, 15)
    login_as(client, coordinator_id())
    for day, utc_hour in ((winter, 17), (summer, 16)):
        req = make_request(school["id"], teacher["id"], day=day)
        client.post(f"/requests/{req['id']}/approve", data=approve_data((day, "09:00", "11:00", 2)))
        (shift,) = shifts_of(req["id"])
        assert shift["start_at"].tzinfo is not None  # timestamptz, not a plain timestamp
        assert shift["start_at"] == datetime(year, day.month, 15, utc_hour, 0, tzinfo=timezone.utc)
        assert shift["end_at"] == datetime(year, day.month, 15, utc_hour + 2, 0, tzinfo=timezone.utc)


def test_coordinator_filters(client, school, teacher):
    other_school = make_school()
    other_teacher = make_user("teacher", school_id=other_school["id"])
    a = make_request(school["id"], teacher["id"], status="submitted")
    b = make_request(other_school["id"], other_teacher["id"], status="rejected")
    login_as(client, coordinator_id())

    page = client.get(f"/requests?status=rejected&school={other_school['id']}").data
    assert f"/requests/{b['id']}\"".encode() in page and f"/requests/{a['id']}\"".encode() not in page
    page = client.get(f"/requests?school={school['id']}").data
    assert f"/requests/{a['id']}\"".encode() in page and f"/requests/{b['id']}\"".encode() not in page
    # Unknown filter values are ignored rather than causing an error.
    assert client.get("/requests?status=bogus").status_code == 200


# ---------- teacher permissions ----------

def test_school_id_comes_from_account_not_form(client, school, teacher):
    other_school = make_school()
    login_as(client, teacher["id"])
    # The teacher's form has no school field at all.
    assert b'name="school_id"' not in client.get("/requests/new").data

    data = request_form(activity="TEST injected")
    data["school_id"] = str(other_school["id"])  # hand-built POST
    assert client.post("/requests/new", data=data).status_code == 302
    row = one("SELECT school_id FROM help_requests WHERE activity = 'TEST injected' AND teacher_id = %s",
              (teacher["id"],))
    assert row["school_id"] == school["id"]


# ---------- coordinator submits ----------

def test_coordinator_can_submit_for_any_school(client, school):
    other_school = make_school()
    login_as(client, coordinator_id())
    page = client.get("/requests/new").data
    assert b'name="school_id"' in page and other_school["name"].encode() in page

    for target in (school, other_school):
        data = {**request_form(activity="TEST by coordinator"), "school_id": str(target["id"])}
        resp = client.post("/requests/new", data=data)
        assert resp.status_code == 302
        row = one("SELECT * FROM help_requests WHERE activity = 'TEST by coordinator' AND school_id = %s",
                  (target["id"],))
        assert row["teacher_id"] == coordinator_id()   # records who asked
        assert row["status"] == "submitted"


@pytest.mark.parametrize("school_value", ["", "0", "999999999", "not-a-number"])
def test_coordinator_must_choose_a_real_school(client, school_value):
    login_as(client, coordinator_id())
    before = one("SELECT count(*) AS n FROM help_requests")["n"]
    data = {**request_form(activity="TEST no school"), "school_id": school_value}
    resp = client.post("/requests/new", data=data)
    assert resp.status_code == 200
    assert one("SELECT count(*) AS n FROM help_requests")["n"] == before


def test_teacher_cannot_view_or_edit_other_schools_request(client, teacher):
    school_b = make_school()
    teacher_b = make_user("teacher", school_id=school_b["id"])
    req_b = make_request(school_b["id"], teacher_b["id"], status="changes_requested", note="fix it")
    login_as(client, teacher["id"])

    assert client.get(f"/requests/{req_b['id']}").status_code == 403
    assert client.get(f"/requests/{req_b['id']}/edit").status_code == 403
    assert client.post(f"/requests/{req_b['id']}/edit", data=request_form()).status_code == 403
    assert f"/requests/{req_b['id']}\"".encode() not in client.get("/requests").data
    assert get_request(req_b["id"])["status"] == "changes_requested"


@pytest.mark.parametrize("status", ["submitted", "approved", "rejected"])
def test_teacher_cannot_edit_unless_changes_requested(client, school, teacher, status):
    req = make_request(school["id"], teacher["id"], status=status)
    login_as(client, teacher["id"])
    assert client.get(f"/requests/{req['id']}/edit").status_code == 403
    assert client.post(f"/requests/{req['id']}/edit", data=request_form(needed=9)).status_code == 403
    after = get_request(req["id"])
    assert after["status"] == status and after["volunteers_needed"] == 2


def test_teacher_cannot_use_coordinator_actions(client, school, teacher):
    req = make_request(school["id"], teacher["id"])
    shift = make_shift(school["id"])
    login_as(client, teacher["id"])
    before = counts()
    day = req["date"]
    assert client.post(f"/requests/{req['id']}/approve",
                       data=approve_data((day, "09:00", "11:00", 2))).status_code == 403
    assert client.post(f"/requests/{req['id']}/send-back", data={"note": "x"}).status_code == 403
    assert client.post(f"/requests/{req['id']}/reject", data={}).status_code == 403
    assert client.post(f"/shifts/{shift['id']}/cancel").status_code == 403
    assert counts() == before


def test_volunteer_gets_403_everywhere(app, client, school, teacher):
    req = make_request(school["id"], teacher["id"], status="changes_requested", note="n")
    shift = make_shift(school["id"], request_id=req["id"])
    volunteer = make_user("volunteer")
    login_as(client, volunteer["id"])
    post_data = {**request_form(), **approve_data((req["date"], "09:00", "11:00", 2)), "note": "x"}
    assert_forbidden_everywhere(app, client, "help_requests",
                                url_args={"request_id": req["id"], "shift_id": shift["id"]},
                                post_data=post_data)


# ---------- invalid input ----------

@pytest.mark.parametrize("changes", [
    {"start_time": "11:00", "end_time": "09:00"},   # end before start
    {"start_time": "09:00", "end_time": "09:00"},   # zero-length
    {"volunteers_needed": "0"},
    {"date": past().isoformat()},
    {"activity": ""},
])
def test_invalid_request_saves_nothing(client, school, teacher, changes):
    login_as(client, teacher["id"])
    data = {**request_form(activity="TEST invalid"), **changes}
    resp = client.post("/requests/new", data=data)
    assert resp.status_code == 200
    assert one("SELECT count(*) AS n FROM help_requests WHERE teacher_id = %s", (teacher["id"],))["n"] == 0


def test_invalid_resubmission_saves_nothing(client, school, teacher):
    req = make_request(school["id"], teacher["id"], status="changes_requested", note="n")
    login_as(client, teacher["id"])
    client.post(f"/requests/{req['id']}/edit", data=request_form(start="11:00", end="10:00"))
    assert get_request(req["id"])["status"] == "changes_requested"


@pytest.mark.parametrize("row", [
    ("today+20", "11:00", "09:00", 2),   # end before start
    ("today+20", "09:00", "11:00", 0),   # no volunteers
    ("past", "09:00", "11:00", 2),       # past date
])
def test_invalid_shift_rows_create_nothing(client, school, teacher, row):
    req = make_request(school["id"], teacher["id"])
    day = future(20) if row[0] == "today+20" else past()
    good = (future(20), "13:00", "14:00", 1)
    login_as(client, coordinator_id())
    resp = client.post(f"/requests/{req['id']}/approve", data=approve_data(good, (day, *row[1:])))
    assert resp.status_code == 400
    assert shifts_of(req["id"]) == []  # the valid row isn't saved either
    assert get_request(req["id"])["status"] == "submitted"


def test_send_back_requires_note(client, school, teacher):
    req = make_request(school["id"], teacher["id"])
    login_as(client, coordinator_id())
    resp = client.post(f"/requests/{req['id']}/send-back", data={"note": "   "})
    assert resp.status_code == 400
    assert get_request(req["id"])["status"] == "submitted"


# ---------- status transitions ----------

@pytest.mark.parametrize("status,action", [
    ("rejected", "approve"),
    ("approved", "approve"),            # no second set of shifts
    ("approved", "reject"),
    ("approved", "send-back"),
    ("changes_requested", "approve"),   # must be resubmitted first
    ("changes_requested", "reject"),
    ("rejected", "send-back"),
])
def test_invalid_transitions_refused(client, school, teacher, status, action):
    req = make_request(school["id"], teacher["id"], status=status, note="old note")
    login_as(client, coordinator_id())
    data = {"approve": approve_data((req["date"], "09:00", "11:00", 2)),
            "reject": {"reject_note": "no"}, "send-back": {"note": "change it"}}[action]
    before = counts()
    resp = client.post(f"/requests/{req['id']}/{action}", data=data)
    assert resp.status_code == 409
    assert counts() == before


def test_reject_submitted_request(client, school, teacher):
    req = make_request(school["id"], teacher["id"])
    login_as(client, coordinator_id())
    assert client.post(f"/requests/{req['id']}/reject", data={"reject_note": "No room that day"}).status_code == 302
    after = get_request(req["id"])
    assert after["status"] == "rejected" and after["coordinator_note"] == "No room that day"


def test_cancel_shift(client, school, teacher):
    shift = make_shift(school["id"])
    login_as(client, coordinator_id())
    assert client.post(f"/shifts/{shift['id']}/cancel").status_code == 302
    assert one("SELECT status FROM shifts WHERE id = %s", (shift["id"],))["status"] == "cancelled"
    # Cancelling again is refused.
    assert client.post(f"/shifts/{shift['id']}/cancel").status_code == 409


def test_missing_request_and_shift_are_404(client):
    login_as(client, coordinator_id())
    assert client.get("/requests/999999999").status_code == 404
    assert client.post("/shifts/999999999/cancel").status_code == 404

"""Hours and reports (PRD rule 9): only 'present' sign-ups count, never stored."""
import re
from datetime import timedelta

import pytest

from tests.helpers import (coordinator_id, login_as, make_school, make_shift, make_signup,
                           make_user)

pytestmark = pytest.mark.usefixtures("cleanup_test_data")


# ---------- helpers ----------

def past_shift(school, hours=2.0, days_ago=3):
    """A shift that already happened, `hours` long."""
    return make_shift(school["id"], start_in=timedelta(days=-days_ago), hours=hours)


def volunteer():
    return make_user("volunteer", status="active")


def worked(vol, school, hours=2.0, status="present", days_ago=3):
    """Give a volunteer a past shift at a school with the given sign-up status."""
    shift = past_shift(school, hours=hours, days_ago=days_ago)
    make_signup(shift["id"], vol["id"], status=status)
    return shift


def page_total(html):
    """The big total number at the top of an hours page."""
    match = re.search(rb'class="total-hours"><span>([0-9.]+)</span>', html)
    assert match, "no total on the page"
    return match.group(1).decode()


def table_row(html, label):
    """The text of the table row containing `label` (a school name or email)."""
    for row in re.findall(rb"<tr>(.*?)</tr>", html, flags=re.S):
        if label.encode() in row:
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", row.decode())).strip()
    return None


def row_hours(html, label):
    row = table_row(html, label)
    assert row is not None, f"{label} not in the table"
    return row.split()[-1]  # hours is the last column


@pytest.fixture
def school():
    return make_school()


# ---------- only 'present' counts ----------

def test_only_present_counts(client, school):
    vol = volunteer()
    worked(vol, school, hours=2, status="present", days_ago=10)
    for i, status in enumerate(["confirmed", "waitlisted", "cancelled", "no_show"]):
        worked(vol, school, hours=3, status=status, days_ago=3 + i)

    login_as(client, vol["id"])
    page = client.get("/my-hours")
    assert page.status_code == 200
    assert page_total(page.data) == "2"
    assert page.data.count(b'<td class="num">') == 1  # one present shift listed

    login_as(client, coordinator_id())
    report = client.get("/reports").data
    assert row_hours(report, vol["email"]) == "2"
    assert row_hours(report, school["name"]) == "2"


def test_volunteer_with_no_present_shifts_has_zero(client, school):
    vol = volunteer()
    worked(vol, school, status="no_show")
    login_as(client, vol["id"])
    assert page_total(client.get("/my-hours").data) == "0"
    login_as(client, coordinator_id())
    assert row_hours(client.get("/reports").data, vol["email"]) == "0"


# ---------- totals ----------

def test_two_hours_plus_one_and_a_half_is_three_and_a_half(client, school):
    vol = volunteer()
    worked(vol, school, hours=2, days_ago=3)
    worked(vol, school, hours=1.5, days_ago=4)

    login_as(client, vol["id"])
    page = client.get("/my-hours").data
    assert page_total(page) == "3.5"
    assert b'<td class="num">1.5</td>' in page and b'<td class="num">2</td>' in page

    login_as(client, coordinator_id())
    report = client.get("/reports").data
    assert row_hours(report, vol["email"]) == "3.5"
    assert row_hours(report, school["name"]) == "3.5"
    assert page_total(client.get(f"/hours/{vol['id']}").data) == "3.5"


def test_school_total_adds_up_volunteers(client, school):
    a, b = volunteer(), volunteer()
    worked(a, school, hours=2)
    worked(b, school, hours=1.5)
    login_as(client, coordinator_id())
    row = table_row(client.get("/reports").data, school["name"])
    assert row.split()[-3:] == ["2", "2", "3.5"]  # volunteers, shifts, hours


# ---------- permissions ----------

def test_volunteer_cannot_open_another_volunteers_hours(client, school):
    a, b = volunteer(), volunteer()
    worked(b, school)
    login_as(client, a["id"])
    assert client.get(f"/hours/{b['id']}").status_code == 403
    assert client.get(f"/hours/{a['id']}").status_code == 403  # even their own: use /my-hours
    assert client.get("/reports").status_code == 403


def test_teacher_cannot_see_hours_at_another_school(client, school):
    school_b = make_school()
    teacher_a = make_user("teacher", school_id=school["id"])
    only_b = volunteer()
    worked(only_b, school_b, hours=2)
    both = volunteer()
    worked(both, school, hours=1.5)
    worked(both, school_b, hours=2, days_ago=5)

    login_as(client, teacher_a["id"])
    # A volunteer who only worked at school B: 403.
    assert client.get(f"/hours/{only_b['id']}").status_code == 403
    # Unknown ids look the same as "not at your school".
    assert client.get("/hours/999999999").status_code == 403

    # A volunteer who worked at both: only school A's hours.
    page = client.get(f"/hours/{both['id']}").data
    assert page_total(page) == "1.5"
    assert school_b["name"].encode() not in page

    # The report never mentions school B or its volunteers.
    report = client.get("/reports")
    assert report.status_code == 200
    assert school_b["name"].encode() not in report.data
    assert only_b["email"].encode() not in report.data
    assert row_hours(report.data, both["email"]) == "1.5"
    assert row_hours(report.data, school["name"]) == "1.5"


def test_teacher_sees_volunteer_with_only_unmarked_signup_at_school(client, school):
    """'Has had a shift there' means any sign-up, not just present ones."""
    teacher = make_user("teacher", school_id=school["id"])
    vol = volunteer()
    worked(vol, school, status="confirmed")
    login_as(client, teacher["id"])
    page = client.get(f"/hours/{vol['id']}")
    assert page.status_code == 200
    assert page_total(page.data) == "0"


# ---------- coordinator ----------

def test_coordinator_sees_all_schools(client, school):
    busy, quiet = school, make_school()
    vol = volunteer()
    worked(vol, busy, hours=2)
    login_as(client, coordinator_id())
    report = client.get("/reports").data
    assert row_hours(report, busy["name"]) == "2"
    assert row_hours(report, quiet["name"]) == "0"  # schools with no hours still listed
    assert f'href="/hours/{vol["id"]}"'.encode() in report


def test_coordinator_hours_page_for_non_volunteer_is_404(client, school):
    teacher = make_user("teacher", school_id=school["id"])
    login_as(client, coordinator_id())
    assert client.get(f"/hours/{teacher['id']}").status_code == 404
    assert client.get("/hours/999999999").status_code == 404


# ---------- logged out ----------

@pytest.mark.parametrize("url", ["/my-hours", "/hours/1", "/reports"])
def test_logged_out_visitor_redirected(client, url):
    resp = client.get(url)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")

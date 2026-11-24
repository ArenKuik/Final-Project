"""Coordinator school management: create, edit, regenerate invite codes, permissions."""
import re

import pytest

from tests.helpers import (PASSWORD, assert_forbidden_everywhere, coordinator_id, login_as,
                           make_school, make_user, new_email, one, random_school_name)
from volunteerconnect.schools import INVITE_ALPHABET

pytestmark = pytest.mark.usefixtures("cleanup_test_data")


@pytest.fixture
def coordinator(client):
    login_as(client, coordinator_id())
    return client


def get_school_by_name(name):
    return one("SELECT * FROM schools WHERE name = %s", (name,))


def register_teacher(client, code):
    email = new_email()
    client.post("/register", data={"email": email, "password": PASSWORD,
                                   "role": "teacher", "invite_code": code})
    return one("SELECT * FROM users WHERE email = %s", (email,))


def test_coordinator_sees_school_list(coordinator):
    school = make_school()
    resp = coordinator.get("/schools")
    assert resp.status_code == 200
    assert school["name"].encode() in resp.data
    assert school["invite_code"].encode() in resp.data


def test_create_school_generates_invite_code(coordinator):
    name = random_school_name()
    resp = coordinator.post("/schools/new", data={
        "name": name, "address": "1 Main St", "required_clearances": ["tb_test", "background_check"],
    })
    assert resp.status_code == 302

    school = get_school_by_name(name)
    assert school["address"] == "1 Main St"
    assert sorted(school["required_clearances"]) == ["background_check", "tb_test"]
    code = school["invite_code"]
    assert len(code) == 10 and set(code) <= set(INVITE_ALPHABET)
    assert one("SELECT count(*) AS n FROM schools WHERE invite_code = %s", (code,))["n"] == 1


def test_invite_codes_are_different(coordinator):
    names = [random_school_name() for _ in range(3)]
    for name in names:
        coordinator.post("/schools/new", data={"name": name})
    codes = {get_school_by_name(n)["invite_code"] for n in names}
    assert len(codes) == 3


def test_edit_school(coordinator):
    school = make_school(required=["tb_test"])
    new_name = random_school_name()
    # Unticking every checkbox sends no required_clearances at all.
    resp = coordinator.post(f"/schools/{school['id']}/edit", data={"name": new_name, "address": "2 Oak Ave"})
    assert resp.status_code == 302

    updated = one("SELECT * FROM schools WHERE id = %s", (school["id"],))
    assert updated["name"] == new_name
    assert updated["address"] == "2 Oak Ave"
    assert updated["required_clearances"] == []
    assert updated["invite_code"] == school["invite_code"]


def test_edit_form_shows_saved_values(coordinator):
    school = make_school(required=["background_check"])
    resp = coordinator.get(f"/schools/{school['id']}/edit")
    assert resp.status_code == 200
    assert school["name"].encode() in resp.data
    checkbox = lambda value: re.search(rb'<input[^>]*value="' + value + rb'"[^>]*>', resp.data).group(0)
    assert b"checked" in checkbox(b"background_check")
    assert b"checked" not in checkbox(b"tb_test")


def test_regenerate_code(coordinator, app):
    school = make_school()
    old_code = school["invite_code"]
    existing_teacher = make_user("teacher", school_id=school["id"])

    resp = coordinator.post(f"/schools/{school['id']}/regenerate-code")
    assert resp.status_code == 302
    new_code = one("SELECT invite_code FROM schools WHERE id = %s", (school["id"],))["invite_code"]
    assert new_code != old_code

    # Teachers already registered keep their school.
    teacher = one("SELECT school_id FROM users WHERE id = %s", (existing_teacher["id"],))
    assert teacher["school_id"] == school["id"]

    # Registration needs a logged-out client.
    visitor = app.test_client()
    visitor.environ_base["wsgi.url_scheme"] = "https"
    assert register_teacher(visitor, old_code) is None
    new_teacher = register_teacher(visitor, new_code)
    assert new_teacher is not None and new_teacher["school_id"] == school["id"]


def test_missing_school_name_saves_nothing(coordinator):
    before = one("SELECT count(*) AS n FROM schools")["n"]
    resp = coordinator.post("/schools/new", data={"name": "", "address": "TEST address"})
    assert resp.status_code == 200
    assert b"This field is required" in resp.data
    assert one("SELECT count(*) AS n FROM schools")["n"] == before


def test_unknown_clearance_type_saves_nothing(coordinator):
    name = random_school_name()
    resp = coordinator.post("/schools/new", data={"name": name, "required_clearances": ["tb_test", "fingerprints"]})
    assert resp.status_code == 200
    assert get_school_by_name(name) is None

    school = make_school(required=["tb_test"])
    coordinator.post(f"/schools/{school['id']}/edit", data={"name": school["name"], "required_clearances": ["fingerprints"]})
    assert one("SELECT required_clearances FROM schools WHERE id = %s", (school["id"],))["required_clearances"] == ["tb_test"]


def test_missing_school_is_404(coordinator):
    assert coordinator.get("/schools/999999999/edit").status_code == 404
    assert coordinator.post("/schools/999999999/regenerate-code").status_code == 404


@pytest.mark.parametrize("role", ["teacher", "volunteer"])
def test_non_coordinators_get_403(app, client, role):
    school = make_school()
    user = make_user(role, school_id=school["id"] if role == "teacher" else None)
    login_as(client, user["id"])
    assert_forbidden_everywhere(
        app, client, "schools",
        url_args={"school_id": school["id"]},
        post_data={"name": random_school_name(), "required_clearances": ["tb_test"]},
    )

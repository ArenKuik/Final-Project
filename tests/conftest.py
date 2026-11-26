"""Shared test setup. Tests run against the database in DATABASE_URL
(use a separate Neon branch for testing, not production)."""
import os

import pytest
from dotenv import load_dotenv

load_dotenv()


def pytest_collection_modifyitems(config, items):
    if not os.environ.get("DATABASE_URL"):
        skip = pytest.mark.skip(reason="DATABASE_URL is not set; add it to .env to run database tests")
        for item in items:
            item.add_marker(skip)


# ---------- shared fixtures for coordinator features ----------
# (test_auth.py defines its own `app`/`client`, which take priority there.)

@pytest.fixture(scope="session")
def app():
    from volunteerconnect import create_app
    return create_app({"TESTING": True, "WTF_CSRF_ENABLED": False})


@pytest.fixture
def client(app):
    client = app.test_client()
    # Pretend every request is HTTPS so the Secure session cookie is sent back.
    client.environ_base["wsgi.url_scheme"] = "https"
    return client


@pytest.fixture
def cleanup_test_data():
    yield
    from tests.helpers import delete_test_data
    delete_test_data()

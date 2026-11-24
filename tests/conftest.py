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

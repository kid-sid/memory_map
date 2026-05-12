"""Shared pytest fixtures."""

import re
import pytest
import history_store


def _re_escape(path: str) -> str:
    """Escape a file path for use as a MongoDB $regex prefix."""
    return re.escape(path)


@pytest.fixture
def requires_mongodb():
    """Skip the test if MongoDB is not configured."""
    if history_store._get_collection() is None:
        pytest.skip("MEMORY_MAP_MONGO_URI not set — MongoDB unavailable")


@pytest.fixture(autouse=True)
def mongo_cleanup(tmp_path):
    """Delete any MongoDB documents written by this test after it finishes.

    Uses tmp_path as a project-path prefix filter so only test-generated
    documents are removed — real project data is never touched.
    """
    yield
    col = history_store._get_collection()
    if col is not None:
        col.delete_many({"project": {"$regex": f"^{_re_escape(str(tmp_path))}"}})

"""Shared pytest fixtures."""

import re
import pytest
import history_store


def _re_escape(path: str) -> str:
    """Escape a file path for use as a MongoDB $regex prefix."""
    return re.escape(path)


@pytest.fixture
def json_mode(monkeypatch):
    """Force history_store to use the JSON fallback regardless of MEMORY_MAP_MONGO_URI.

    Temporarily clears the URI env var and resets the module-level MongoDB
    connection cache so _get_collection() re-runs with no URI and falls back
    to the JSON file.  monkeypatch auto-restores everything after the test.
    """
    monkeypatch.delenv("MEMORY_MAP_MONGO_URI", raising=False)
    monkeypatch.setattr(history_store, "_mongo_col", None)
    monkeypatch.setattr(history_store, "_mongo_init_done", False)
    yield
    # Force a clean re-init for the next test (monkeypatch has already restored
    # the original attribute values, but we reset again to be safe).
    history_store._mongo_init_done = False
    history_store._mongo_col = None


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

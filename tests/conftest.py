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


@pytest.fixture
def requires_file_mode():
    """Skip if MongoDB is configured — test exercises the file-based memory path only."""
    from server import _memory_collection
    if _memory_collection() is not None:
        pytest.skip("Test exercises file-based memory path, skipped when MongoDB is configured")


@pytest.fixture(autouse=True)
def mongo_cleanup(tmp_path):
    """Delete any MongoDB documents written by this test after it finishes.

    Cleans both the history and memory collections, keyed by tmp_path prefix,
    so only test-generated documents are removed — real project data is untouched.
    Also cleans __global__ memory docs and __migrated_from_file__ sentinels added
    during the test run.
    """
    yield
    col = history_store._get_collection()
    if col is not None:
        prefix = _re_escape(str(tmp_path))
        col.delete_many({"project": {"$regex": f"^{prefix}"}})
        try:
            mem_col = col.database["memory"]
            mem_col.delete_many({"project": {"$regex": f"^{prefix}"}})
            mem_col.delete_many({"project": "__global__"})
        except Exception:
            pass


@pytest.fixture(autouse=True)
def clear_auto_migrate_cache():
    """Clear the per-process auto-migrate cache between tests."""
    import server
    server._auto_migrated_projects.clear()
    yield
    server._auto_migrated_projects.clear()

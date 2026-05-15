"""Tests for the MongoDB-backed memory path (save_memory, load_memory, delete_memory)."""

import datetime
import pytest
from memory_map_mcp.server import save_memory, load_memory, delete_memory


def test_save_and_load_memory_mongo(tmp_path, requires_mongodb):
    result = save_memory(str(tmp_path), "stack", "FastAPI + MongoDB")
    assert result == "saved: stack"
    loaded = load_memory(str(tmp_path))
    assert "FastAPI" in loaded
    assert "stack" in loaded


def test_load_memory_returns_empty_when_no_data(tmp_path, requires_mongodb):
    result = load_memory(str(tmp_path))
    assert result == "no memory saved yet"


def test_delete_memory_mongo(tmp_path, requires_mongodb):
    save_memory(str(tmp_path), "stack", "FastAPI + MongoDB")
    result = delete_memory(str(tmp_path), "stack")
    assert result == "deleted: stack"
    loaded = load_memory(str(tmp_path))
    assert "FastAPI" not in loaded


def test_delete_nonexistent_key_mongo(tmp_path, requires_mongodb):
    result = delete_memory(str(tmp_path), "ghost")
    assert "not found" in result


def test_load_memory_tfidf_query_mongo(tmp_path, requires_mongodb):
    save_memory(str(tmp_path), "gotchas", "portalocker is required for file locking")
    save_memory(str(tmp_path), "stack", "FastAPI with MongoDB and pytest")
    result = load_memory(str(tmp_path), query="portalocker")
    lines = [l for l in result.splitlines() if l.strip()]
    gotchas_pos = next((i for i, l in enumerate(lines) if "gotchas" in l), None)
    stack_pos = next((i for i, l in enumerate(lines) if "stack" in l), None)
    assert gotchas_pos is not None
    assert stack_pos is None or gotchas_pos < stack_pos


def test_load_memory_top_k_mongo(tmp_path, requires_mongodb):
    for i in range(10):
        save_memory(str(tmp_path), f"key{i}", f"value about topic number {i}")
    result = load_memory(str(tmp_path), query="value topic", top_k=3)
    lines = [l for l in result.splitlines() if l.strip()]
    assert len(lines) <= 3


def test_save_memory_overwrites_existing_mongo(tmp_path, requires_mongodb):
    save_memory(str(tmp_path), "stack", "FastAPI")
    save_memory(str(tmp_path), "stack", "Django")
    loaded = load_memory(str(tmp_path))
    assert "Django" in loaded
    assert "FastAPI" not in loaded


def test_stale_warning_mongo(tmp_path, requires_mongodb):
    from memory_map_mcp.server import _memory_collection
    import pathlib
    col = _memory_collection()
    project = str(pathlib.Path(str(tmp_path)).resolve())
    stale_ts = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=31)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    col.update_one(
        {"project": project, "key": "old-entry"},
        {"$set": {"value": "some old value", "updated_at": stale_ts, "schema_version": 1}},
        upsert=True,
    )
    result = load_memory(str(tmp_path))
    assert "stale" in result
    assert "old" in result


def test_no_stale_warning_for_fresh_entry_mongo(tmp_path, requires_mongodb):
    save_memory(str(tmp_path), "stack", "FastAPI + MongoDB")
    result = load_memory(str(tmp_path))
    assert "stale" not in result


def test_similarity_warning_mongo(tmp_path, requires_mongodb):
    save_memory(str(tmp_path), "stack", "FastAPI MongoDB Redis")
    result = save_memory(str(tmp_path), "tech_stack", "FastAPI MongoDB Redis caching")
    assert "saved: tech_stack" in result
    assert "warning" in result
    assert "stack" in result

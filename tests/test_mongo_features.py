"""Tests for MongoDB-only features: compression, global memory, migration tool, auto-migrate."""

import datetime
import pathlib
import json
import pytest

from memory_map_mcp.server import (
    save_memory,
    load_memory,
    delete_memory,
    set_compression,
    save_global_memory,
    load_global_memory,
    migrate_memory_to_mongo,
    _memory_collection,
    _normalize_project_path,
    MEMORY_FILE,
    COMPRESSION_KEY,
    _GLOBAL_SENTINEL,
)


# ---------------------------------------------------------------------------
# Compression in MongoDB (#59)
# ---------------------------------------------------------------------------

def test_set_compression_mongo(tmp_path, requires_mongodb):
    result = set_compression(str(tmp_path), 2)
    assert result == "compression set to 2"
    from memory_map_mcp.server import _read_compression_level
    assert _read_compression_level(str(tmp_path)) == 2


def test_compression_persists_in_load_memory(tmp_path, requires_mongodb):
    set_compression(str(tmp_path), 2)
    save_memory(str(tmp_path), "entry_point", "main.py")
    result = load_memory(str(tmp_path))
    assert "[entry]" in result


def test_compression_default_when_not_set(tmp_path, requires_mongodb):
    from memory_map_mcp.server import _read_compression_level
    assert _read_compression_level(str(tmp_path)) == 1


def test_compression_not_visible_as_user_key(tmp_path, requires_mongodb):
    set_compression(str(tmp_path), 2)
    save_memory(str(tmp_path), "stack", "FastAPI")
    result = load_memory(str(tmp_path))
    assert COMPRESSION_KEY not in result
    assert "FastAPI" in result


# ---------------------------------------------------------------------------
# Global memory in MongoDB (#60)
# ---------------------------------------------------------------------------

def test_save_and_load_global_memory_mongo(requires_mongodb):
    save_global_memory("testkey_global", "TestValue123")
    result = load_global_memory()
    assert "GLOBAL" in result
    assert "TestValue123" in result


def test_global_memory_stored_under_sentinel(requires_mongodb):
    col = _memory_collection()
    save_global_memory("sentinelcheck", "sentinelvalue")
    doc = col.find_one({"project": _GLOBAL_SENTINEL, "key": "sentinelcheck"})
    assert doc is not None
    assert doc["value"] == "sentinelvalue"


def test_global_memory_not_in_project_queries(tmp_path, requires_mongodb):
    save_global_memory("globalkey", "globalvalue")
    result = load_cross_project_memory_helper(tmp_path)
    assert "globalvalue" not in result


def test_global_memory_excluded_from_list_projects(tmp_path, requires_mongodb):
    save_global_memory("gkey", "gval")
    result = json.loads(list_projects_helper(tmp_path))
    for p in result:
        assert p.get("path") != _GLOBAL_SENTINEL


def load_cross_project_memory_helper(tmp_path):
    from memory_map_mcp.server import load_cross_project_memory
    return load_cross_project_memory(str(tmp_path))


def list_projects_helper(tmp_path):
    from memory_map_mcp.server import list_projects
    return list_projects(str(tmp_path))


# ---------------------------------------------------------------------------
# Migration tool (#62)
# ---------------------------------------------------------------------------

def test_migrate_memory_no_mongo_configured(tmp_path, monkeypatch):
    monkeypatch.setattr("memory_map_mcp.server._memory_col", None)
    monkeypatch.setattr("memory_map_mcp.server._memory_col_init_done", True)
    result = migrate_memory_to_mongo(str(tmp_path))
    assert "error" in result
    assert "MongoDB" in result


def test_migrate_memory_no_file(tmp_path, requires_mongodb):
    result = migrate_memory_to_mongo(str(tmp_path))
    assert "error" in result
    assert "not found" in result


def test_migrate_memory_dry_run(tmp_path, requires_mongodb):
    mem_file = tmp_path / MEMORY_FILE
    mem_file.write_text(json.dumps({"stack": "FastAPI", "_updated_stack": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    result = migrate_memory_to_mongo(str(tmp_path), dry_run=True)
    assert "dry-run" in result
    assert "migrated: 1" in result
    col = _memory_collection()
    doc = col.find_one({"project": _normalize_project_path(str(tmp_path)), "key": "stack"})
    assert doc is None


def test_migrate_memory_writes_to_mongo(tmp_path, requires_mongodb):
    mem_file = tmp_path / MEMORY_FILE
    mem_file.write_text(json.dumps({"stack": "FastAPI", "_updated_stack": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    result = migrate_memory_to_mongo(str(tmp_path))
    assert "migrated: 1" in result
    col = _memory_collection()
    doc = col.find_one({"project": _normalize_project_path(str(tmp_path)), "key": "stack"})
    assert doc is not None
    assert doc["value"] == "FastAPI"


def test_migrate_memory_skips_existing_without_force(tmp_path, requires_mongodb):
    save_memory(str(tmp_path), "stack", "ExistingValue")
    mem_file = tmp_path / MEMORY_FILE
    mem_file.write_text(json.dumps({"stack": "FileValue"}), encoding="utf-8")
    result = migrate_memory_to_mongo(str(tmp_path))
    assert "skipped" in result
    loaded = load_memory(str(tmp_path))
    assert "ExistingValue" in loaded


def test_migrate_memory_force_overwrites(tmp_path, requires_mongodb):
    save_memory(str(tmp_path), "stack", "ExistingValue")
    mem_file = tmp_path / MEMORY_FILE
    mem_file.write_text(json.dumps({"stack": "FileValue"}), encoding="utf-8")
    migrate_memory_to_mongo(str(tmp_path), force=True)
    loaded = load_memory(str(tmp_path))
    assert "FileValue" in loaded


def test_migrate_memory_writes_sentinel(tmp_path, requires_mongodb):
    mem_file = tmp_path / MEMORY_FILE
    mem_file.write_text(json.dumps({"stack": "FastAPI"}), encoding="utf-8")
    migrate_memory_to_mongo(str(tmp_path))
    col = _memory_collection()
    sentinel = col.find_one({"project": _normalize_project_path(str(tmp_path)), "key": "__migrated_from_file__"})
    assert sentinel is not None


def test_migrate_memory_creates_backup(tmp_path, requires_mongodb):
    mem_file = tmp_path / MEMORY_FILE
    mem_file.write_text(json.dumps({"stack": "FastAPI"}), encoding="utf-8")
    migrate_memory_to_mongo(str(tmp_path))
    bak_files = list(tmp_path.glob(".mcp_memory.json.bak.*"))
    assert len(bak_files) == 1


def test_migrate_global_memory(requires_mongodb, tmp_path, monkeypatch):
    global_file = tmp_path / ".mcp_global_memory.json"
    global_file.write_text(json.dumps({"name": "Sidhartha"}), encoding="utf-8")
    monkeypatch.setattr("memory_map_mcp.server.GLOBAL_MEMORY_FILE", global_file)
    result = migrate_memory_to_mongo("__global__")
    assert "migrated: 1" in result
    col = _memory_collection()
    doc = col.find_one({"project": _GLOBAL_SENTINEL, "key": "name"})
    assert doc is not None
    assert doc["value"] == "Sidhartha"


# ---------------------------------------------------------------------------
# Auto-migrate (#63)
# ---------------------------------------------------------------------------

def test_auto_migrate_on_load_memory(tmp_path, requires_mongodb):
    import memory_map_mcp.server as server
    mem_file = tmp_path / MEMORY_FILE
    mem_file.write_text(json.dumps({"stack": "AutoMigratedValue", "_updated_stack": "2026-01-01T00:00:00Z"}), encoding="utf-8")
    server._auto_migrated_projects.clear()
    result = load_memory(str(tmp_path))
    assert "AutoMigratedValue" in result
    col = _memory_collection()
    doc = col.find_one({"project": _normalize_project_path(str(tmp_path)), "key": "stack"})
    assert doc is not None


def test_auto_migrate_does_not_overwrite_existing(tmp_path, requires_mongodb):
    import memory_map_mcp.server as server
    save_memory(str(tmp_path), "stack", "MongoValue")
    mem_file = tmp_path / MEMORY_FILE
    mem_file.write_text(json.dumps({"stack": "FileValue"}), encoding="utf-8")
    server._auto_migrated_projects.clear()
    col = _memory_collection()
    col.delete_many({"project": _normalize_project_path(str(tmp_path)), "key": "__migrated_from_file__"})
    load_memory(str(tmp_path))
    loaded = load_memory(str(tmp_path))
    assert "MongoValue" in loaded


def test_auto_migrate_only_runs_once(tmp_path, requires_mongodb):
    import memory_map_mcp.server as server
    mem_file = tmp_path / MEMORY_FILE
    mem_file.write_text(json.dumps({"stack": "Value"}), encoding="utf-8")
    server._auto_migrated_projects.clear()
    load_memory(str(tmp_path))
    project = _normalize_project_path(str(tmp_path))
    assert project in server._auto_migrated_projects
    save_memory(str(tmp_path), "stack", "AfterMigrate")
    load_memory(str(tmp_path))
    assert project in server._auto_migrated_projects


def test_auto_migrate_skips_when_sentinel_exists(tmp_path, requires_mongodb):
    import memory_map_mcp.server as server
    col = _memory_collection()
    project = _normalize_project_path(str(tmp_path))
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    col.update_one(
        {"project": project, "key": "__migrated_from_file__"},
        {"$set": {"value": now, "schema_version": 1}},
        upsert=True,
    )
    mem_file = tmp_path / MEMORY_FILE
    mem_file.write_text(json.dumps({"stack": "ShouldNotMigrate"}), encoding="utf-8")
    server._auto_migrated_projects.clear()
    load_memory(str(tmp_path))
    doc = col.find_one({"project": project, "key": "stack"})
    assert doc is None

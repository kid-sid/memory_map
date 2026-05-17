import pytest
import json
import datetime
from memory_map_mcp.server import (
    save_memory, load_memory, delete_memory,
    _load_json_safe, _read_memory, _write_memory,
    _memory_collection, _normalize_project_path,
)
import pathlib


def test_valid_key(tmp_path):
    result = save_memory(str(tmp_path), "my-key_123", "value")
    assert result == "saved: my-key_123"


def test_reserved_key_rejected(tmp_path):
    result = save_memory(str(tmp_path), "_system", "value")
    assert result.startswith("error")
    assert "reserved" in result


def test_invalid_key_spaces(tmp_path):
    result = save_memory(str(tmp_path), "key with spaces", "value")
    assert result.startswith("error")


def test_invalid_key_special_chars(tmp_path):
    result = save_memory(str(tmp_path), "key@name!", "value")
    assert result.startswith("error")


def test_invalid_key_too_long(tmp_path):
    result = save_memory(str(tmp_path), "a" * 101, "value")
    assert result.startswith("error")


def test_valid_key_with_dash_and_underscore(tmp_path):
    result = save_memory(str(tmp_path), "my-key_name", "value")
    assert result == "saved: my-key_name"


def test_content_size_limit(tmp_path):
    big_content = "x" * (11 * 1024)
    result = save_memory(str(tmp_path), "bigkey", big_content)
    assert result.startswith("error")
    assert "KB" in result


def test_content_within_limit(tmp_path):
    small_content = "x" * (5 * 1024)
    result = save_memory(str(tmp_path), "smallkey", small_content)
    assert result == "saved: smallkey"


def test_corrupted_memory_json_recovery(tmp_path):
    mem_file = tmp_path / ".mcp_memory.json"
    mem_file.write_text("{ invalid json !!!", encoding="utf-8")
    result = load_memory(str(tmp_path))
    assert "error" not in result
    assert "no memory saved yet" in result


def test_corrupted_memory_json_creates_backup(tmp_path):
    # Tests _load_json_safe directly — always file-based, regardless of MongoDB.
    mem_file = tmp_path / ".mcp_memory.json"
    mem_file.write_text("{ invalid json !!!", encoding="utf-8")
    _read_memory(str(tmp_path))
    bak_files = list(tmp_path.glob(".mcp_memory.json.bak.*"))
    assert len(bak_files) == 1


def test_load_json_safe_missing_file(tmp_path):
    result = _load_json_safe(tmp_path / "nonexistent.json", {"default": "value"})
    assert result == {"default": "value"}


def test_load_json_safe_valid_file(tmp_path):
    f = tmp_path / "good.json"
    f.write_text(json.dumps({"key": "val"}), encoding="utf-8")
    result = _load_json_safe(f, {})
    assert result == {"key": "val"}


def test_load_json_safe_corrupted_creates_backup(tmp_path):
    f = tmp_path / "bad.json"
    f.write_text("not valid json", encoding="utf-8")
    result = _load_json_safe(f, {"default": "value"})
    assert result == {"default": "value"}
    bak_files = list(tmp_path.glob("bad.json.bak.*"))
    assert len(bak_files) == 1


def test_delete_existing_key(tmp_path):
    project = str(tmp_path)
    save_memory(project, "mykey", "myvalue")
    result = delete_memory(project, "mykey")
    assert result == "deleted: mykey"
    result = load_memory(project)
    assert "myval" not in result


def test_delete_nonexistent_key(tmp_path):
    result = delete_memory(str(tmp_path), "ghost")
    assert "not found" in result


def test_save_memory_writes_timestamp(tmp_path):
    save_memory(str(tmp_path), "stack", "FastAPI + MongoDB")
    col = _memory_collection()
    if col is not None:
        project = _normalize_project_path(str(tmp_path))
        doc = col.find_one({"project": project, "key": "stack"})
        assert doc is not None and "updated_at" in doc
        ts = datetime.datetime.fromisoformat(doc["updated_at"].replace("Z", "+00:00"))
    else:
        data = _read_memory(str(tmp_path))
        assert "_updated_stack" in data
        ts = datetime.datetime.fromisoformat(data["_updated_stack"].replace("Z", "+00:00"))
    age = datetime.datetime.now(datetime.timezone.utc) - ts
    assert age.total_seconds() < 5


def test_load_memory_stale_warning(tmp_path):
    save_memory(str(tmp_path), "stack", "FastAPI + MongoDB")
    stale_ts = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=31)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Back-date the timestamp to 31 days ago in whichever storage is active.
    col = _memory_collection()
    if col is not None:
        project = _normalize_project_path(str(tmp_path))
        col.update_one({"project": project, "key": "stack"}, {"$set": {"updated_at": stale_ts}})
    else:
        data = _read_memory(str(tmp_path))
        data["_updated_stack"] = stale_ts
        _write_memory(str(tmp_path), data)
    result = load_memory(str(tmp_path))
    assert "stale" in result
    assert "31d old" in result


def test_load_memory_no_stale_warning_for_fresh_entry(tmp_path):
    save_memory(str(tmp_path), "stack", "FastAPI + MongoDB")
    result = load_memory(str(tmp_path))
    assert "stale" not in result


def test_delete_memory_removes_timestamp(tmp_path):
    save_memory(str(tmp_path), "stack", "FastAPI + MongoDB")
    delete_memory(str(tmp_path), "stack")
    data = _read_memory(str(tmp_path))
    assert "_updated_stack" not in data


def test_load_memory_tfidf_rare_term_ranks_above_generic(tmp_path):
    # "portalocker" is a rare distinctive term; "project" is common to both entries
    save_memory(str(tmp_path), "gotchas", "portalocker is required for file locking in this project")
    save_memory(str(tmp_path), "stack", "FastAPI project with MongoDB and pytest")
    result = load_memory(str(tmp_path), query="portalocker")
    lines = [l for l in result.splitlines() if l.strip()]
    # gotchas should appear before stack since portalocker is unique to it
    gotchas_pos = next((i for i, l in enumerate(lines) if "gotchas" in l), None)
    stack_pos = next((i for i, l in enumerate(lines) if "stack" in l), None)
    assert gotchas_pos is not None, "gotchas entry missing from result"
    assert stack_pos is None or gotchas_pos < stack_pos, "rare-term entry should rank above generic entry"


def test_load_memory_tfidf_top_k_limits_results(tmp_path):
    for i in range(15):
        save_memory(str(tmp_path), f"key{i}", f"value about topic number {i}")
    result = load_memory(str(tmp_path), query="value topic", top_k=5)
    lines = [l for l in result.splitlines() if l.strip()]
    assert len(lines) <= 5


# ---------------------------------------------------------------------------
# save_memory similarity-warning tests (#36)
# ---------------------------------------------------------------------------

def test_save_memory_warns_on_similar_value(tmp_path):
    # "FastAPI MongoDB Redis" and "FastAPI MongoDB Redis caching" share 3/4 words → Jaccard 0.75
    save_memory(str(tmp_path), "stack", "FastAPI MongoDB Redis")
    result = save_memory(str(tmp_path), "tech_stack", "FastAPI MongoDB Redis caching")
    assert "saved: tech_stack" in result
    assert "warning" in result
    assert "stack" in result


def test_save_memory_no_warning_for_distinct_value(tmp_path):
    save_memory(str(tmp_path), "stack", "FastAPI with MongoDB")
    result = save_memory(str(tmp_path), "gotchas", "portalocker required for file locking on windows")
    assert "warning" not in result


def test_save_memory_no_warning_on_first_entry(tmp_path):
    result = save_memory(str(tmp_path), "stack", "FastAPI with MongoDB")
    assert "warning" not in result


def test_save_memory_no_self_warning_on_update(tmp_path):
    save_memory(str(tmp_path), "stack", "FastAPI with MongoDB")
    result = save_memory(str(tmp_path), "stack", "FastAPI with MongoDB and Redis")
    assert "warning" not in result


def test_delete_wildcard_key_does_not_bulk_delete(tmp_path):
    save_memory(str(tmp_path), "stack", "FastAPI")
    save_memory(str(tmp_path), "gotchas", "portalocker required")
    result = delete_memory(str(tmp_path), "*")
    assert result.startswith("error") or "not found" in result
    loaded = load_memory(str(tmp_path))
    assert "FastAPI" in loaded

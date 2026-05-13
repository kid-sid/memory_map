import pytest
import json
import datetime
from server import save_memory, load_memory, delete_memory, _load_json_safe, _read_memory, _write_memory
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
    data = _read_memory(str(tmp_path))
    assert "_updated_stack" in data
    ts = datetime.datetime.fromisoformat(data["_updated_stack"].replace("Z", "+00:00"))
    age = datetime.datetime.now(datetime.timezone.utc) - ts
    assert age.total_seconds() < 5


def test_load_memory_stale_warning(tmp_path):
    save_memory(str(tmp_path), "stack", "FastAPI + MongoDB")
    # Back-date the timestamp by 31 days to simulate a stale entry
    data = _read_memory(str(tmp_path))
    stale_ts = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
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

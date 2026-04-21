import pytest
import json
from server import save_memory, load_memory, delete_memory, _load_json_safe
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

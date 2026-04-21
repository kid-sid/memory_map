import pytest
import json
from server import save_history, load_history


def test_save_and_load(tmp_path):
    project = str(tmp_path)
    save_history(project, "Fixed auth bug")
    result = load_history(project)
    assert "Fixed auth bug" in result


def test_rolling_window_enforced(tmp_path):
    project = str(tmp_path)
    for i in range(25):
        save_history(project, f"summary {i}")
    hist_file = tmp_path / ".mcp_history.json"
    data = json.loads(hist_file.read_text())
    assert len(data["chunks"]) == 20


def test_load_last_n(tmp_path):
    project = str(tmp_path)
    for i in range(10):
        save_history(project, f"chunk {i}")
    result = load_history(project, last_n=3)
    assert "chunk 9" in result
    assert "chunk 7" in result
    assert "chunk 6" not in result


def test_empty_history(tmp_path):
    result = load_history(str(tmp_path))
    assert result == "no history yet"


def test_chunk_ids_increment(tmp_path):
    project = str(tmp_path)
    save_history(project, "first")
    save_history(project, "second")
    save_history(project, "third")
    hist_file = tmp_path / ".mcp_history.json"
    data = json.loads(hist_file.read_text())
    ids = [c["id"] for c in data["chunks"]]
    assert ids == [1, 2, 3]


def test_session_id_stored(tmp_path):
    project = str(tmp_path)
    save_history(project, "summary", session_id="abc123xyz")
    hist_file = tmp_path / ".mcp_history.json"
    data = json.loads(hist_file.read_text())
    assert data["chunks"][0]["session"] == "abc123xy"


def test_history_header_in_output(tmp_path):
    project = str(tmp_path)
    save_history(project, "some work done")
    result = load_history(project)
    assert "=== Recent History ===" in result

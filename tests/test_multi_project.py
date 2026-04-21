import pytest
import json
import pathlib
from server import (
    save_memory, list_projects, load_cross_project_memory,
    search_across_projects, save_global_memory, load_global_memory,
    get_project_summary, save_history,
)


def setup_project(base: pathlib.Path, name: str, memories: dict) -> pathlib.Path:
    proj = base / name
    proj.mkdir()
    for k, v in memories.items():
        save_memory(str(proj), k, v)
    return proj


# --- list_projects ---

def test_list_projects_finds_memory_projects(tmp_path):
    setup_project(tmp_path, "proj-a", {"stack": "Python"})
    setup_project(tmp_path, "proj-b", {"stack": "Go"})
    result = json.loads(list_projects(str(tmp_path)))
    names = [p["name"] for p in result]
    assert "proj-a" in names
    assert "proj-b" in names


def test_list_projects_excludes_dirs_without_memory(tmp_path):
    setup_project(tmp_path, "proj-a", {"stack": "Python"})
    (tmp_path / "not-a-project").mkdir()
    result = json.loads(list_projects(str(tmp_path)))
    names = [p["name"] for p in result]
    assert "not-a-project" not in names


def test_list_projects_key_count(tmp_path):
    setup_project(tmp_path, "proj-a", {"stack": "Python", "entry": "main.py"})
    result = json.loads(list_projects(str(tmp_path)))
    proj_a = next(p for p in result if p["name"] == "proj-a")
    assert proj_a["key_count"] == 2


def test_list_projects_invalid_path():
    result = json.loads(list_projects("/nonexistent/path/xyz"))
    assert "error" in result


# --- load_cross_project_memory ---

def test_load_cross_project_all_keys(tmp_path):
    setup_project(tmp_path, "proj-a", {"stack": "Python FastAPI"})
    setup_project(tmp_path, "proj-b", {"stack": "Go + Gin"})
    result = load_cross_project_memory(str(tmp_path))
    assert "proj-a" in result
    assert "proj-b" in result
    assert "Python FastAPI" in result
    assert "Go + Gin" in result


def test_load_cross_project_filtered_keys(tmp_path):
    setup_project(tmp_path, "proj-a", {"stack": "Python", "notes": "secret notes"})
    result = load_cross_project_memory(str(tmp_path), query_keys="stack")
    assert "Python" in result
    assert "secret notes" not in result


def test_load_cross_project_no_match_for_filter(tmp_path):
    setup_project(tmp_path, "proj-a", {"stack": "Python"})
    result = load_cross_project_memory(str(tmp_path), query_keys="nonexistent_key")
    assert "no projects with memory found" in result


def test_load_cross_project_empty_base(tmp_path):
    result = load_cross_project_memory(str(tmp_path))
    assert "no projects with memory found" in result


# --- search_across_projects ---

def test_search_finds_match(tmp_path):
    setup_project(tmp_path, "proj-a", {"stack": "Python FastAPI + PostgreSQL"})
    setup_project(tmp_path, "proj-b", {"stack": "Go + MySQL"})
    result = search_across_projects(str(tmp_path), "PostgreSQL")
    assert "proj-a" in result
    assert "proj-b" not in result


def test_search_case_insensitive(tmp_path):
    setup_project(tmp_path, "proj-a", {"stack": "python fastapi"})
    result = search_across_projects(str(tmp_path), "PYTHON")
    assert "proj-a" in result


def test_search_no_matches(tmp_path):
    setup_project(tmp_path, "proj-a", {"stack": "Python"})
    result = search_across_projects(str(tmp_path), "Rust")
    assert "no matches" in result


def test_search_empty_keyword(tmp_path):
    result = search_across_projects(str(tmp_path), "")
    assert result.startswith("error")


def test_search_skips_system_keys(tmp_path):
    proj = tmp_path / "proj-a"
    proj.mkdir()
    mem_file = proj / ".mcp_memory.json"
    import json as _json
    mem_file.write_text(_json.dumps({"_compression": 1, "stack": "Python"}), encoding="utf-8")
    result = search_across_projects(str(tmp_path), "_compression")
    assert "no matches" in result


# --- get_project_summary ---

def test_get_project_summary_basic(tmp_path):
    proj = setup_project(tmp_path, "myproject", {"stack": "Python", "entry": "main.py"})
    save_history(str(proj), "Fixed login bug")
    result = get_project_summary(str(proj))
    assert "myproject" in result
    assert "Keys stored: 2" in result
    assert "Fixed login bug" in result


def test_get_project_summary_no_history(tmp_path):
    proj = setup_project(tmp_path, "myproject", {"stack": "Python"})
    result = get_project_summary(str(proj))
    assert "myproject" in result
    assert "N/A" in result


# --- global memory ---

def test_save_and_load_global_memory(tmp_path, monkeypatch):
    global_file = tmp_path / ".mcp_global_memory.json"
    monkeypatch.setattr("server.GLOBAL_MEMORY_FILE", global_file)
    save_global_memory("name", "Sidhartha")
    result = load_global_memory()
    assert "GLOBAL" in result
    assert "Sidhartha" in result


def test_global_memory_empty(tmp_path, monkeypatch):
    global_file = tmp_path / ".mcp_global_memory.json"
    monkeypatch.setattr("server.GLOBAL_MEMORY_FILE", global_file)
    result = load_global_memory()
    assert "no global memory saved yet" in result


def test_global_memory_key_validation(tmp_path, monkeypatch):
    global_file = tmp_path / ".mcp_global_memory.json"
    monkeypatch.setattr("server.GLOBAL_MEMORY_FILE", global_file)
    result = save_global_memory("_reserved", "value")
    assert result.startswith("error")


def test_global_memory_persists_across_calls(tmp_path, monkeypatch):
    global_file = tmp_path / ".mcp_global_memory.json"
    monkeypatch.setattr("server.GLOBAL_MEMORY_FILE", global_file)
    save_global_memory("lang", "Python")
    save_global_memory("editor", "neovim")
    result = load_global_memory()
    assert "Python" in result
    assert "neovim" in result

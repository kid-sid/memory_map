"""Tests for: query filtering in load_memory, max_depth in multi-project tools,
and pathspec-based gitignore handling in get_local_structure."""
import json
import pathlib

import pytest

from server import (
    get_local_structure,
    list_projects,
    load_cross_project_memory,
    load_memory,
    save_memory,
    search_across_projects,
)


def setup_project(base: pathlib.Path, name: str, memories: dict) -> pathlib.Path:
    proj = base / name
    proj.mkdir(parents=True)
    for k, v in memories.items():
        save_memory(str(proj), k, v)
    return proj


# ---------------------------------------------------------------------------
# load_memory — query filtering (issue #2)
# ---------------------------------------------------------------------------

def test_load_memory_query_returns_matching_entries(tmp_path):
    save_memory(str(tmp_path), "stack", "Python FastAPI")
    save_memory(str(tmp_path), "gotchas", "never import db directly")
    result = load_memory(str(tmp_path), query="Python")
    assert "Python FastAPI" in result
    assert "never import db" not in result


def test_load_memory_query_matches_key_name(tmp_path):
    save_memory(str(tmp_path), "stack", "FastAPI")
    save_memory(str(tmp_path), "gotchas", "some note")
    result = load_memory(str(tmp_path), query="stack")
    assert "FastAPI" in result
    assert "some note" not in result


def test_load_memory_query_no_match_returns_message(tmp_path):
    save_memory(str(tmp_path), "stack", "Python FastAPI")
    result = load_memory(str(tmp_path), query="Rust")
    assert "no matching memory entries" in result


def test_load_memory_empty_query_returns_all(tmp_path):
    save_memory(str(tmp_path), "stack", "Python")
    save_memory(str(tmp_path), "notes", "remember this")
    result = load_memory(str(tmp_path), query="")
    assert "Python" in result
    assert "remember this" in result


def test_load_memory_query_case_insensitive(tmp_path):
    save_memory(str(tmp_path), "stack", "python fastapi")
    result = load_memory(str(tmp_path), query="PYTHON")
    assert "python fastapi" in result


def test_load_memory_query_multiple_keywords_any_match(tmp_path):
    save_memory(str(tmp_path), "stack", "Go + Gin")
    save_memory(str(tmp_path), "notes", "auth via JWT")
    result = load_memory(str(tmp_path), query="Go auth")
    assert "Go + Gin" in result
    assert "auth via JWT" in result


# ---------------------------------------------------------------------------
# list_projects / load_cross_project_memory / search_across_projects
# — max_depth (issue #4)
# ---------------------------------------------------------------------------

def test_list_projects_depth1_misses_nested(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    setup_project(sub, "deep-proj", {"stack": "Python"})
    result = json.loads(list_projects(str(tmp_path)))
    names = [p["name"] for p in result]
    assert "deep-proj" not in names


def test_list_projects_depth2_finds_nested(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    setup_project(sub, "deep-proj", {"stack": "Python"})
    result = json.loads(list_projects(str(tmp_path), max_depth=2))
    names = [p["name"] for p in result]
    assert "deep-proj" in names


def test_list_projects_depth1_still_finds_direct_children(tmp_path):
    setup_project(tmp_path, "proj-a", {"stack": "Python"})
    result = json.loads(list_projects(str(tmp_path), max_depth=1))
    names = [p["name"] for p in result]
    assert "proj-a" in names


def test_list_projects_max_depth_capped_at_5(tmp_path):
    result = json.loads(list_projects(str(tmp_path), max_depth=99))
    assert isinstance(result, list)


def test_load_cross_project_depth2_finds_nested(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    setup_project(sub, "deep-proj", {"stack": "Rust"})
    result = load_cross_project_memory(str(tmp_path), max_depth=2)
    assert "Rust" in result


def test_load_cross_project_depth1_misses_nested(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    setup_project(sub, "deep-proj", {"stack": "Rust"})
    result = load_cross_project_memory(str(tmp_path), max_depth=1)
    assert "Rust" not in result


def test_search_across_projects_depth2_finds_nested(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    setup_project(sub, "deep-proj", {"stack": "Elixir Phoenix"})
    result = search_across_projects(str(tmp_path), "Elixir", max_depth=2)
    assert "deep-proj" in result


def test_search_across_projects_depth1_misses_nested(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    setup_project(sub, "deep-proj", {"stack": "Elixir Phoenix"})
    result = search_across_projects(str(tmp_path), "Elixir", max_depth=1)
    assert "no matches" in result


# ---------------------------------------------------------------------------
# get_local_structure — pathspec gitignore (issue #3)
# ---------------------------------------------------------------------------

def _flat_files(tree: dict | list) -> list[str]:
    """Recursively collect all file names from the tree structure."""
    if isinstance(tree, list):
        return tree
    files = list(tree.get("files", []))
    for k, v in tree.items():
        if k != "files":
            files.extend(_flat_files(v))
    return files


def test_gitignore_wildcard_pattern(tmp_path):
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (tmp_path / "app.py").touch()
    (tmp_path / "debug.log").touch()
    result = json.loads(get_local_structure(str(tmp_path)))
    files = _flat_files(result[tmp_path.name])
    assert "app.py" in files
    assert "debug.log" not in files


def test_gitignore_directory_pattern(tmp_path):
    (tmp_path / ".gitignore").write_text("dist/\n", encoding="utf-8")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "bundle.js").touch()
    (tmp_path / "main.py").touch()
    result = json.loads(get_local_structure(str(tmp_path)))
    tree = result[tmp_path.name]
    if isinstance(tree, dict):
        assert "dist" not in tree
    files = _flat_files(tree)
    assert "main.py" in files


def test_gitignore_recursive_glob(tmp_path):
    (tmp_path / ".gitignore").write_text("**/*.pyc\n", encoding="utf-8")
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "module.pyc").touch()
    (sub / "module.py").touch()
    result = json.loads(get_local_structure(str(tmp_path)))
    files = _flat_files(result[tmp_path.name])
    assert "module.pyc" not in files
    assert "module.py" in files


def test_gitignore_no_gitignore_file(tmp_path):
    (tmp_path / "main.py").touch()
    result = json.loads(get_local_structure(str(tmp_path)))
    files = _flat_files(result[tmp_path.name])
    assert "main.py" in files

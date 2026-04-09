from fastmcp import FastMCP
import pathlib
import httpx
import json
import os
import fnmatch
import subprocess

mcp = FastMCP("file-structure")

DEFAULT_IGNORE = {".git", "node_modules", "__pycache__", ".venv", "dist", ".next", ".mypy_cache", ".pytest_cache"}


def load_gitignore_patterns(root_path: pathlib.Path) -> list[str]:
    """Load patterns from a .gitignore file in the root path."""
    gitignore_path = root_path / ".gitignore"
    patterns = []
    if gitignore_path.exists():
        with open(gitignore_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    # Basic .gitignore to fnmatch conversion
                    if line.endswith("/"):
                        patterns.append(line[:-1])
                        patterns.append(line + "*")
                    else:
                        patterns.append(line)
    return patterns


def is_ignored(entry_name: str, patterns: list[str]) -> bool:
    """Check if an entry should be ignored based on patterns."""
    if entry_name in DEFAULT_IGNORE:
        return True
    for pattern in patterns:
        if fnmatch.fnmatch(entry_name, pattern):
            return True
    return False


# ---------------------------------------------------------------------------
# Tool: get_local_structure
# ---------------------------------------------------------------------------

def build_local_tree(dir_path: pathlib.Path, max_depth: int, patterns: list[str], current_depth: int = 0) -> "dict | list":
    """Recursively build a JSON-friendly tree for a local directory."""
    try:
        entries = sorted(dir_path.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        entries = [e for e in entries if not is_ignored(e.name, patterns)]
    except PermissionError:
        return []

    if current_depth >= max_depth:
        return [e.name for e in entries]

    files = []
    subdirs = {}

    for entry in entries:
        if entry.is_file():
            files.append(entry.name)
        elif entry.is_dir():
            subdirs[entry.name] = build_local_tree(entry, max_depth, patterns, current_depth + 1)

    if not subdirs:
        return files

    result = dict(subdirs)
    if files:
        result["files"] = files
    return result


@mcp.tool()
def get_local_structure(path: str, max_depth: int = 5) -> str:
    """Get the file/folder structure of a local directory as minified JSON."""
    root = pathlib.Path(path)
    if not root.exists():
        return json.dumps({"error": f"Path '{path}' does not exist"})
    if not root.is_dir():
        return json.dumps({"error": f"'{path}' is not a directory"})

    patterns = load_gitignore_patterns(root)
    tree = {root.name: build_local_tree(root, max_depth, patterns)}
    return json.dumps(tree)


# ---------------------------------------------------------------------------
# Tool: get_github_structure
# ---------------------------------------------------------------------------

def build_github_tree(items: list, max_depth: int) -> dict:
    """Build a nested JSON tree from items returned by GitHub's recursive tree API."""
    root: dict = {}
    dirs = sorted([i for i in items if i["type"] == "tree"], key=lambda x: x["path"])
    files = sorted([i for i in items if i["type"] == "blob"], key=lambda x: x["path"])

    for item in dirs + files:
        parts = item["path"].split("/")
        if len(parts) > max_depth:
            continue

        current = root
        for part in parts[:-1]:
            if not isinstance(current.get(part), dict):
                current[part] = {}
            current = current[part]

        name = parts[-1]
        if item["type"] == "blob":
            current.setdefault("files", []).append(name)
        else:
            current.setdefault(name, {})

    return root


@mcp.tool()
def get_github_structure(repo: str, branch: str = "main") -> str:
    """Get the file/folder structure of a GitHub repository as minified JSON."""
    if "/" not in repo:
        return json.dumps({"error": "repo must be in 'owner/repo' format"})

    url = f"https://api.github.com/repos/{repo}/git/trees/{branch}?recursive=1"
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = httpx.get(url, headers=headers, timeout=15)
    except httpx.RequestError as e:
        return json.dumps({"error": f"Request failed: {e}"})

    if resp.status_code == 404:
        return json.dumps({"error": f"Repo '{repo}' or branch '{branch}' not found"})
    if resp.status_code == 403:
        return json.dumps({"error": "Rate limited — set GITHUB_TOKEN env var for higher limits"})
    if resp.status_code != 200:
        return json.dumps({"error": f"GitHub API returned {resp.status_code}"})

    data = resp.json()
    tree = build_github_tree(data.get("tree", []), max_depth=5)
    result = {repo: tree}
    if data.get("truncated"):
        result["_note"] = "Tree truncated by GitHub — repo may be too large"

    return json.dumps(result)


# ---------------------------------------------------------------------------
# Tool: get_git_history
# ---------------------------------------------------------------------------

@mcp.tool()
def get_git_history(path: str, count: int = 5) -> str:
    """Get the recent git commit history for a local repository as minified JSON."""
    root = pathlib.Path(path)
    if not root.exists() or not root.is_dir():
        return json.dumps({"error": f"Invalid path: {path}"})

    SEP = "\x1f"  # ASCII unit separator — never appears in commit messages

    try:
        # Check if it's a git repo
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path, check=True, capture_output=True, text=True, timeout=5
        )

        # Get logs in format: hash<SEP>subject
        result = subprocess.run(
            ["git", "log", f"-n{count}", f"--pretty=format:%H{SEP}%s"],
            cwd=path, check=True, capture_output=True, text=True, timeout=10
        )

        lines = []
        if result.stdout.strip():
            for line in result.stdout.splitlines():
                parts = line.split(SEP, 1)
                if len(parts) == 2:
                    lines.append(f"{parts[0][:8]} | {parts[1]}")

        return "\n".join(lines)

    except subprocess.TimeoutExpired:
        return json.dumps({"error": "Git command timed out"})
    except subprocess.CalledProcessError as e:
        return json.dumps({"error": f"Git command failed: {e.stderr.strip() or str(e)}"})
    except Exception as e:
        return json.dumps({"error": str(e)})


# ---------------------------------------------------------------------------
# Tool: Memory management
# ---------------------------------------------------------------------------

MEMORY_FILE = ".mcp_memory.json"


def _memory_path(project_path: str) -> pathlib.Path:
    return pathlib.Path(project_path) / MEMORY_FILE


def _read_memory(project_path: str) -> dict:
    p = _memory_path(project_path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_memory(project_path: str, data: dict) -> None:
    p = _memory_path(project_path)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


@mcp.tool()
def save_memory(project_path: str, key: str, content: str) -> str:
    """Save or update a memory entry for a project. Use short keys (e.g. 'stack', 'architecture', 'gotchas')."""
    try:
        data = _read_memory(project_path)
        data[key] = content
        _write_memory(project_path, data)
        return f"saved: {key}"
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def load_memory(project_path: str) -> str:
    """Load all saved memory for a project. Call this at the start of every session."""
    try:
        data = _read_memory(project_path)
        if not data:
            return "no memory saved yet"
        return "\n".join(f"{k}: {v}" for k, v in data.items())
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def delete_memory(project_path: str, key: str) -> str:
    """Delete a specific memory entry for a project."""
    try:
        data = _read_memory(project_path)
        if key not in data:
            return f"key '{key}' not found"
        del data[key]
        _write_memory(project_path, data)
        return f"deleted: {key}"
    except Exception as e:
        return f"error: {e}"


if __name__ == "__main__":
    mcp.run()

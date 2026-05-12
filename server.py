from fastmcp import FastMCP
import pathlib
import httpx
import json
import os
import fnmatch
import subprocess
import re
import datetime
import tempfile

import portalocker
import history_store

mcp = FastMCP("file-structure")

DEFAULT_IGNORE = {".git", "node_modules", "__pycache__", ".venv", "dist", ".next", ".mypy_cache", ".pytest_cache"}

MEMORY_FILE = ".mcp_memory.json"
HISTORY_FILE = ".mcp_history.json"
MAX_HISTORY_CHUNKS = 20
COMPRESSION_KEY = "_compression"
DEFAULT_COMPRESSION = 1
KEY_PATTERN = re.compile(r'^[a-zA-Z0-9_-]{1,100}$')
MAX_ENTRY_KB = int(os.environ.get("MCP_MAX_ENTRY_KB", "10"))
GIT_TIMEOUT = int(os.environ.get("MCP_GIT_TIMEOUT", "10"))
GLOBAL_MEMORY_FILE = pathlib.Path.home() / ".mcp_global_memory.json"
MAX_LOCAL_DEPTH = 10  # hard cap on get_local_structure depth

_WORKSPACE_ROOT: pathlib.Path | None = (
    pathlib.Path(os.environ["MCP_WORKSPACE_ROOT"]).resolve()
    if "MCP_WORKSPACE_ROOT" in os.environ else None
)


def _validate_project_path(path: str) -> pathlib.Path:
    """Resolve path and enforce MCP_WORKSPACE_ROOT if configured.

    Always resolves symlinks and '..' components so callers never get an
    un-normalised path.  When MCP_WORKSPACE_ROOT is set every resolved path
    must be equal to or a sub-directory of that root.
    """
    resolved = pathlib.Path(path).resolve()
    if _WORKSPACE_ROOT is not None and not (
        resolved == _WORKSPACE_ROOT or resolved.is_relative_to(_WORKSPACE_ROOT)
    ):
        raise ValueError(
            f"path '{path}' resolves to '{resolved}' which is outside the "
            f"allowed workspace root '{_WORKSPACE_ROOT}'"
        )
    return resolved

ABBREVIATIONS = {
    "python": "py", "javascript": "js", "typescript": "ts",
    "kubernetes": "k8s", "database": "db", "repository": "repo",
    "application": "app", "server": "srv", "configuration": "config",
    "environment": "env", "directory": "dir", "function": "fn",
    "library": "lib", "authentication": "auth",
    "development": "dev", "production": "prod",
    "dependencies": "deps", "dependency": "dep",
    "frontend": "fe", "backend": "be",
    "commands": "cmds", "command": "cmd",
    "project": "proj", "description": "desc",
    "languages": "langs", "language": "lang",
    "packages": "pkgs", "package": "pkg",
    "documentation": "docs", "document": "doc",
    "implementation": "impl",
}

FILLER_PATTERNS = [
    r"(?<![/\\])\bthe\s+",
    r"(?<![/\\])\ba\s+(?![/\\])",
    r"(?<![/\\])\ban\s+",
    r"\bis\s+",
    r"\bare\s+",
]

KEY_ABBREVIATIONS = {
    "architecture": "arch",
    "entry_point": "entry",
    "canonical_format": "canon",
    "current_work": "wip",
    "conventions": "conv",
    "structure": "struct",
    "environment_variables": "env",
    "env_vars": "env",
    "testing": "test",
    "deployment": "deploy",
}


# ---------------------------------------------------------------------------
# Shared JSON helpers
# ---------------------------------------------------------------------------

def _load_json_safe(path: pathlib.Path, default: dict) -> dict:
    """Load JSON from path; on corruption, back up the file and return default."""
    if not path.exists():
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = path.parent / f"{path.name}.bak.{ts}"
        try:
            path.rename(backup)
        except OSError:
            pass
        return default
    except OSError:
        return default


def _locked_write(path: pathlib.Path, data: dict) -> None:
    """Write JSON to path atomically under an exclusive lock.

    Writes to a temp file in the same directory, then renames over the target.
    The lock file is a sidecar (.lock) so the target is never truncated before
    the new content is fully written.
    """
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a", encoding="utf-8") as lf:
        portalocker.lock(lf, portalocker.LOCK_EX)
        try:
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                os.replace(tmp, path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        finally:
            portalocker.unlock(lf)


# ---------------------------------------------------------------------------
# Tool: get_local_structure
# ---------------------------------------------------------------------------

def load_gitignore_patterns(root_path: pathlib.Path) -> list[str]:
    gitignore_path = root_path / ".gitignore"
    patterns = []
    if gitignore_path.exists():
        with open(gitignore_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    if line.endswith("/"):
                        patterns.append(line[:-1])
                        patterns.append(line + "*")
                    else:
                        patterns.append(line)
    return patterns


def is_ignored(entry_name: str, patterns: list[str]) -> bool:
    if entry_name in DEFAULT_IGNORE:
        return True
    for pattern in patterns:
        if fnmatch.fnmatch(entry_name, pattern):
            return True
    return False


def build_local_tree(dir_path: pathlib.Path, max_depth: int, patterns: list[str], current_depth: int = 0) -> "dict | list":
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
    max_depth = min(max_depth, MAX_LOCAL_DEPTH)
    try:
        root = _validate_project_path(path)
    except ValueError as e:
        return json.dumps({"error": str(e)})
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
    """Get the recent git commit history for a local repository.

    Timeout is controlled by MCP_GIT_TIMEOUT env var (default 10s).
    """
    root = pathlib.Path(path)
    if not root.exists() or not root.is_dir():
        return json.dumps({"error": f"Invalid path: {path}"})

    SEP = "\x1f"

    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path, check=True, capture_output=True, text=True, timeout=GIT_TIMEOUT
        )

        result = subprocess.run(
            ["git", "log", f"-n{count}", f"--pretty=format:%H{SEP}%s"],
            cwd=path, check=True, capture_output=True, text=True, timeout=GIT_TIMEOUT * 2
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
# Memory helpers
# ---------------------------------------------------------------------------

def _memory_path(project_path: str) -> pathlib.Path:
    return _validate_project_path(project_path) / MEMORY_FILE


def _history_path(project_path: str) -> pathlib.Path:
    return _validate_project_path(project_path) / HISTORY_FILE


def _read_memory(project_path: str) -> dict:
    return _load_json_safe(_memory_path(project_path), {})


def _write_memory(project_path: str, data: dict) -> None:
    _locked_write(_memory_path(project_path), data)


def _read_compression_level(project_path: str) -> int:
    data = _load_json_safe(_memory_path(project_path), {})
    level = data.get(COMPRESSION_KEY, DEFAULT_COMPRESSION)
    return max(0, min(2, int(level)))


def _shorten_key(key: str) -> str:
    return KEY_ABBREVIATIONS.get(key, key)


def _abbreviate(text: str) -> str:
    result = text
    for full, short in ABBREVIATIONS.items():
        result = re.sub(r'\b' + re.escape(full) + r'\b', short, result, flags=re.IGNORECASE)
    for pattern in FILLER_PATTERNS:
        result = re.sub(pattern, '', result, flags=re.IGNORECASE)
    result = re.sub(r'\s{2,}', ' ', result).strip()
    return result


def _compress_memory(data: dict, level: int = 1) -> str:
    """Compress memory dict into LLM-optimized text.

    Level 0 (raw):     key: value
    Level 1 (compact): [key] value
    Level 2 (dense):   [shortened_key] abbreviated_value
    """
    entries = {k: v for k, v in data.items() if not k.startswith("_")}

    if not entries:
        return "no memory saved yet"

    if level == 0:
        return "\n".join(f"{k}: {v}" for k, v in entries.items())

    lines = []
    for key, value in entries.items():
        short_key = _shorten_key(key) if level >= 2 else key
        compressed_value = _abbreviate(value) if level >= 2 else value
        lines.append(f"[{short_key}] {compressed_value}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools: Memory management
# ---------------------------------------------------------------------------

@mcp.tool()
def save_memory(project_path: str, key: str, content: str) -> str:
    """Save or update a memory entry for a project. Use short keys (e.g. 'stack', 'architecture')."""
    if key.startswith("_"):
        return "error: keys starting with '_' are reserved for system use"
    if not KEY_PATTERN.match(key):
        return "error: key must be 1-100 chars using only letters, digits, _ or -"
    if len(content.encode("utf-8")) > MAX_ENTRY_KB * 1024:
        return f"error: content exceeds {MAX_ENTRY_KB}KB limit (set MCP_MAX_ENTRY_KB to override)"
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
        if not any(not k.startswith("_") for k in data):
            return "no memory saved yet"
        level = max(0, min(2, int(data.get(COMPRESSION_KEY, DEFAULT_COMPRESSION))))
        return _compress_memory(data, level)
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def delete_memory(project_path: str, key: str) -> str:
    """Delete a specific memory entry for a project."""
    if key.startswith("_"):
        return "error: keys starting with '_' are reserved and cannot be deleted"
    try:
        data = _read_memory(project_path)
        if key not in data:
            return f"key '{key}' not found"
        del data[key]
        _write_memory(project_path, data)
        return f"deleted: {key}"
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def set_compression(project_path: str, level: int) -> str:
    """Set the compression level for memory output (0=raw, 1=compact, 2=dense)."""
    if level not in (0, 1, 2):
        return "error: level must be 0, 1, or 2"
    try:
        data = _read_memory(project_path)
        data[COMPRESSION_KEY] = level
        _write_memory(project_path, data)
        return f"compression set to {level}"
    except Exception as e:
        return f"error: {e}"


# ---------------------------------------------------------------------------
# Tools: Conversation history
# ---------------------------------------------------------------------------

@mcp.tool()
def save_history(project_path: str, summary: str, session_id: str = "", tags: str = "") -> str:
    """Save a conversation chunk with auto-extracted tags and token stats.

    tags: optional comma-separated override, e.g. "bug-fix,database".
    If omitted, tags are extracted from summary content by keyword matching.
    """
    try:
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        if not tag_list:
            tag_list = history_store.extract_tags(summary)
        chunk_id = history_store.save_chunk(project_path, session_id[:8] if session_id else "", summary, tag_list)
        tag_str = ",".join(tag_list) if tag_list else "untagged"
        return f"history saved: chunk {chunk_id} tags:[{tag_str}]"
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def load_history(project_path: str, last_n: int = 5) -> str:
    """Load the tag index for recent history chunks. Call at session start for continuity.

    Returns a lightweight index (id, timestamp, tags, preview, token cost) — no full
    dialogue. Call get_history_chunks(ids) to fetch specific chunks in full.
    """
    try:
        index = history_store.load_index(project_path, last_n)
        if not index:
            return "no history yet"

        lines = [
            "=== History Index ===",
            "Call get_history_chunks(project_path, ids) to fetch full dialogue.",
            "",
        ]
        for entry in index:
            tag_str = ",".join(entry.get("tags", [])) or "untagged"
            tokens = entry.get("stats", {}).get("tokens", 0)
            preview = entry.get("preview", "")[:80].replace("\n", " ")
            lines.append(
                f"[{entry['id']}] {entry['timestamp']} tags:[{tag_str}] "
                f"tokens:{tokens} preview:\"{preview}\""
            )
        return "\n".join(lines)
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def get_history_chunks(project_path: str, ids: str) -> str:
    """Fetch full dialogue for the given chunk IDs.

    ids: comma-separated chunk IDs from load_history, e.g. "1,3" or MongoDB ObjectId strings.
    Returns full dialogue per chunk plus total_tokens across all returned chunks.
    """
    try:
        id_list = [i.strip() for i in ids.split(",") if i.strip()]
        if not id_list:
            return "error: no ids provided"
        chunks, total_tokens = history_store.get_chunks(project_path, id_list)
        if not chunks:
            return "no chunks found for the given ids"

        lines = [f"total_tokens: {total_tokens}", ""]
        for chunk in chunks:
            tag_str = ",".join(chunk.get("tags", [])) or "untagged"
            lines.append(
                f"--- [{chunk['id']}] {chunk['timestamp']} tags:[{tag_str}] ---"
            )
            lines.append(chunk.get("dialogue", ""))
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def backfill_history_embeddings(project_path: str = "", batch_size: int = 20) -> str:
    """Generate embeddings for chunks that don't have one yet.

    Useful after enabling vector search for the first time so existing chunks
    become searchable. Pass project_path="" to backfill across all projects.
    Processes batch_size chunks per call — run repeatedly until 'remaining' = 0.

    Only meaningful when MEMORY_MAP_EMBED_PROVIDER=openai. Atlas autoEmbed
    handles its own embeddings automatically.
    """
    try:
        result = history_store.backfill_embeddings(
            project=project_path or None,
            batch_size=batch_size,
        )
        if "reason" in result:
            return f"skipped: {result['reason']}"
        return (
            f"backfilled: {result['backfilled']}, "
            f"failed: {result.get('failed', 0)}, "
            f"remaining: {result['remaining']}"
        )
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def suggest_history(project_path: str, user_message: str, token_budget: int = 2000) -> str:
    """Retrieve the most relevant history chunks for the current task.

    Selection logic (in priority order):
      1. Anchor — the most recent chunk is always included for session continuity.
      2. Vector matches — semantically similar chunks via Atlas Vector Search,
         filtered by min_score, ranked by similarity (high → low).
      3. Recent fill — most recent chunks not already selected, used to fill
         remaining token budget after vector matches.

    Falls back to keyword tag matching when EMBED_PROVIDER is unset or vector
    search returns nothing.

    Call at session start with the user's first message instead of manually
    calling load_history + get_history_chunks.
    """
    try:
        recent_candidates = history_store.load_index(project_path, last_n=10)
        if not recent_candidates:
            return "no history yet"

        # Anchor: most recent chunk, always included for continuity.
        anchor = recent_candidates[0]
        selected_ids = [anchor["id"]]
        score_map = {anchor["id"]: ("anchor", 1.0)}
        used = anchor["stats"].get("tokens", 0)

        # Pool 1 — vector search (semantic). Returns [] for short queries or no provider.
        vector_candidates = history_store.search_by_vector(project_path, user_message, limit=10)

        # Pool 2 — tag matching fallback
        if not vector_candidates:
            msg_tags = history_store.extract_tags(user_message)
            tag_candidates = history_store.query_by_tags(project_path, msg_tags, limit=20)
            scored = history_store.score_chunks(tag_candidates, user_message)
            ranked = [(entry, sc, "tag") for sc, entry in scored if sc > 0]
        else:
            ranked = [(entry, entry.get("score", 0.0), "vector") for entry in vector_candidates]

        # Add ranked candidates by score (high first) within budget.
        for entry, score, source in ranked:
            if entry["id"] in score_map:
                continue
            t = entry["stats"].get("tokens", 0)
            if used + t > token_budget:
                continue
            selected_ids.append(entry["id"])
            score_map[entry["id"]] = (source, score)
            used += t

        # Fill remaining budget with recent chunks not yet selected.
        for entry in recent_candidates:
            if entry["id"] in score_map:
                continue
            t = entry["stats"].get("tokens", 0)
            if used + t > token_budget:
                continue
            selected_ids.append(entry["id"])
            score_map[entry["id"]] = ("recent", 0.0)
            used += t

        chunks, total_tokens = history_store.get_chunks(project_path, selected_ids)
        if not chunks:
            return "no history yet"

        # Present chronologically (oldest first) so the narrative flows naturally.
        chunks.sort(key=lambda c: c.get("timestamp", ""))

        lines = [
            f"=== Relevant History ({len(chunks)} chunks, {total_tokens} tokens) ===",
            "",
        ]
        for chunk in chunks:
            source, score = score_map.get(chunk["id"], ("?", 0.0))
            tag_str = ",".join(chunk.get("tags", [])) or "untagged"
            score_str = f" score={score:.2f}" if source in ("vector", "tag") else ""
            lines.append(f"[{chunk['id']}] {chunk['timestamp']} src={source}{score_str} tags:[{tag_str}]")
            lines.append(chunk.get("dialogue", ""))
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"error: {e}"


# ---------------------------------------------------------------------------
# Tools: Multi-project integration
# ---------------------------------------------------------------------------

@mcp.tool()
def list_projects(base_path: str) -> str:
    """List all projects under base_path that have saved memory, with key counts and last save time."""
    try:
        root = _validate_project_path(base_path)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    if not root.exists() or not root.is_dir():
        return json.dumps({"error": f"'{base_path}' is not a valid directory"})

    projects = []
    try:
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            mem_file = entry / MEMORY_FILE
            if not mem_file.exists():
                continue
            data = _load_json_safe(mem_file, {})
            keys = [k for k in data if not k.startswith("_")]
            last_save = history_store.get_latest_save(str(entry))
            projects.append({
                "path": str(entry),
                "name": entry.name,
                "key_count": len(keys),
                "last_save": last_save,
            })
    except PermissionError as e:
        return json.dumps({"error": str(e)})

    return json.dumps(projects)


@mcp.tool()
def get_project_summary(project_path: str) -> str:
    """Return a summary of a project's stored memory and recent conversation history."""
    try:
        p = _validate_project_path(project_path)
        name = p.name

        mem_data = _read_memory(project_path)
        keys = [k for k in mem_data if not k.startswith("_")]
        compression = mem_data.get(COMPRESSION_KEY, DEFAULT_COMPRESSION)

        last_save = history_store.get_latest_save(project_path)
        recent = history_store.load_index(project_path, last_n=3)

        lines = [
            f"Project: {name}",
            f"Keys stored: {len(keys)}",
            f"Last history save: {last_save or 'N/A'}",
            f"Compression: level {compression}",
        ]
        if recent:
            lines.append("Recent history (last 3):")
            for entry in recent:
                tag_str = ",".join(entry.get("tags", [])) or "untagged"
                lines.append(f"  [{entry['id']}] tags:[{tag_str}] {entry.get('preview', '')[:60]}")

        return "\n".join(lines)
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def load_cross_project_memory(base_path: str, query_keys: str = "") -> str:
    """Load memory from all projects under base_path, optionally filtered by comma-separated keys."""
    try:
        root = _validate_project_path(base_path)
    except ValueError as e:
        return f"error: {e}"
    if not root.exists() or not root.is_dir():
        return f"error: '{base_path}' is not a valid directory"

    filter_keys = {k.strip() for k in query_keys.split(",") if k.strip()} if query_keys else set()

    sections = []
    try:
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            mem_file = entry / MEMORY_FILE
            if not mem_file.exists():
                continue
            data = _load_json_safe(mem_file, {})
            level = max(0, min(2, int(data.get(COMPRESSION_KEY, DEFAULT_COMPRESSION))))
            entries = {k: v for k, v in data.items() if not k.startswith("_")}
            if filter_keys:
                entries = {k: v for k, v in entries.items() if k in filter_keys}
            if not entries:
                continue
            compressed = _compress_memory(entries, level)
            sections.append(f"=== {entry.name} ===\n{compressed}")
    except PermissionError as e:
        return f"error: {e}"

    return "\n\n".join(sections) if sections else "no projects with memory found"


@mcp.tool()
def search_across_projects(base_path: str, keyword: str) -> str:
    """Search memory values across all projects under base_path for a keyword (case-insensitive)."""
    try:
        root = _validate_project_path(base_path)
    except ValueError as e:
        return f"error: {e}"
    if not root.exists() or not root.is_dir():
        return f"error: '{base_path}' is not a valid directory"
    if not keyword.strip():
        return "error: keyword cannot be empty"

    kw = keyword.lower()
    matches = []

    try:
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            mem_file = entry / MEMORY_FILE
            if not mem_file.exists():
                continue
            data = _load_json_safe(mem_file, {})
            for k, v in data.items():
                if k.startswith("_"):
                    continue
                text = str(v)
                if kw in text.lower():
                    pos = text.lower().find(kw)
                    start = max(0, pos - 30)
                    end = min(len(text), pos + 70)
                    prefix = "..." if start > 0 else ""
                    suffix = "..." if end < len(text) else ""
                    window = prefix + text[start:end] + suffix
                    matches.append(f"{entry.name} / {k}: \"{window}\"")
    except PermissionError as e:
        return f"error: {e}"

    return "\n".join(matches) if matches else f"no matches for '{keyword}'"


def _read_global_memory() -> dict:
    return _load_json_safe(GLOBAL_MEMORY_FILE, {})


def _write_global_memory(data: dict) -> None:
    _locked_write(GLOBAL_MEMORY_FILE, data)


@mcp.tool()
def save_global_memory(key: str, content: str) -> str:
    """Save a user-level memory entry available across all projects (e.g. name, preferred stack)."""
    if key.startswith("_"):
        return "error: keys starting with '_' are reserved for system use"
    if not KEY_PATTERN.match(key):
        return "error: key must be 1-100 chars using only letters, digits, _ or -"
    if len(content.encode("utf-8")) > MAX_ENTRY_KB * 1024:
        return f"error: content exceeds {MAX_ENTRY_KB}KB limit"
    try:
        data = _read_global_memory()
        data[key] = content
        _write_global_memory(data)
        return f"global saved: {key}"
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def load_global_memory() -> str:
    """Load user-level memory available across all projects."""
    try:
        data = _read_global_memory()
        entries = {k: v for k, v in data.items() if not k.startswith("_")}
        if not entries:
            return "=== GLOBAL ===\nno global memory saved yet"
        compressed = _compress_memory(data, DEFAULT_COMPRESSION)
        return f"=== GLOBAL ===\n{compressed}"
    except Exception as e:
        return f"error: {e}"


if __name__ == "__main__":
    mcp.run()

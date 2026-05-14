from fastmcp import FastMCP
import pathlib
import httpx
import json
import os
import subprocess
import re
import math
import datetime
import concurrent.futures
import tempfile

import pathspec

import portalocker
import history_store
from redact import redact_secrets

mcp = FastMCP("file-structure")

# Persistent pool — avoids per-call thread creation overhead in suggest_history.
_suggest_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="suggest")

DEFAULT_IGNORE = {".git", "node_modules", "__pycache__", ".venv", "dist", ".next", ".mypy_cache", ".pytest_cache"}

MEMORY_FILE = ".mcp_memory.json"
COMPRESSION_KEY = "_compression"
DEFAULT_COMPRESSION = 1
KEY_PATTERN = re.compile(r'^[a-zA-Z0-9_-]{1,100}$')
MAX_ENTRY_KB = int(os.environ.get("MCP_MAX_ENTRY_KB", "10"))
GIT_TIMEOUT = int(os.environ.get("MCP_GIT_TIMEOUT", "10"))
HISTORY_MAX_TOKENS = int(os.environ.get("MCP_HISTORY_MAX_TOKENS", "50000"))
HISTORY_SUMMARISE_N = int(os.environ.get("MCP_HISTORY_SUMMARISE_N", "10"))
MEMORY_SIMILARITY_THRESHOLD = 0.7
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

def load_gitignore_spec(root_path: pathlib.Path):
    """Return a pathspec.PathSpec for the root .gitignore, or None if absent."""
    gitignore_path = root_path / ".gitignore"
    if not gitignore_path.exists():
        return None
    try:
        lines = gitignore_path.read_text(encoding="utf-8").splitlines()
        return pathspec.PathSpec.from_lines("gitignore", lines)
    except OSError:
        return None


def is_ignored(entry: pathlib.Path, root: pathlib.Path, spec) -> bool:
    if entry.name in DEFAULT_IGNORE:
        return True
    if spec is None:
        return False
    rel = entry.relative_to(root).as_posix()
    return spec.match_file(rel) or (entry.is_dir() and spec.match_file(rel + "/"))


def build_local_tree(dir_path: pathlib.Path, max_depth: int, root: pathlib.Path, spec, current_depth: int = 0) -> "dict | list":
    try:
        entries = sorted(dir_path.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        entries = [e for e in entries if not is_ignored(e, root, spec)]
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
            subdirs[entry.name] = build_local_tree(entry, max_depth, root, spec, current_depth + 1)

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

    spec = load_gitignore_spec(root)
    tree = {root.name: build_local_tree(root, max_depth, root, spec)}
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
def get_github_structure(repo: str, branch: str = "main", max_depth: int = 5) -> str:
    """Get the file/folder structure of a GitHub repository as minified JSON.

    max_depth controls how many directory levels to include (1–10, default 5).
    Use a lower value for large monorepos, higher for small repos needing full detail.
    """
    if "/" not in repo:
        return json.dumps({"error": "repo must be in 'owner/repo' format"})
    if not (1 <= max_depth <= 10):
        return json.dumps({"error": "max_depth must be between 1 and 10"})

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
    tree = build_github_tree(data.get("tree", []), max_depth=max_depth)
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
    try:
        root = _validate_project_path(path)
    except ValueError as e:
        return json.dumps({"error": str(e)})
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

    Entries not updated in >30 days are annotated with a stale warning.
    """
    entries = {k: v for k, v in data.items() if not k.startswith("_")}

    if not entries:
        return "no memory saved yet"

    now = datetime.datetime.now(datetime.timezone.utc)
    _stale_days = 30

    def _stale_suffix(key: str) -> str:
        ts_str = data.get(f"_updated_{key}")
        if not ts_str:
            return ""
        try:
            ts = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            age = now - ts
            if age.days >= _stale_days:
                return f" [stale: {age.days}d old]"
        except ValueError:
            pass
        return ""

    if level == 0:
        return "\n".join(f"{k}: {v}{_stale_suffix(k)}" for k, v in entries.items())

    lines = []
    for key, value in entries.items():
        short_key = _shorten_key(key) if level >= 2 else key
        compressed_value = _abbreviate(value) if level >= 2 else value
        lines.append(f"[{short_key}] {compressed_value}{_stale_suffix(key)}")

    return "\n".join(lines)


def _tfidf_rank(entries: dict, words: list, top_k: int = 10) -> dict:
    """Rank memory entries by TF-IDF relevance to query words.

    Uses smoothed IDF — log((N+1)/(df+1))+1 — so single-entry stores and
    ubiquitous terms still produce meaningful scores. Returns an ordered dict
    of the top_k entries with score > 0, highest score first.
    """
    if not entries or not words:
        return entries

    docs = {k: re.findall(r"[a-z0-9]+", f"{k} {v}".lower()) for k, v in entries.items()}
    N = len(docs)

    df: dict = {}
    for tokens in docs.values():
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1

    scores: dict = {}
    for key, tokens in docs.items():
        total = len(tokens)
        if not total:
            scores[key] = 0.0
            continue
        tf_map: dict = {}
        for t in tokens:
            tf_map[t] = tf_map.get(t, 0) + 1
        score = 0.0
        for word in words:
            tf = tf_map.get(word, 0) / total
            idf = math.log((N + 1) / (df.get(word, 0) + 1)) + 1
            score += tf * idf
        scores[key] = score

    ranked = sorted(
        [(k, s) for k, s in scores.items() if s > 0],
        key=lambda x: x[1],
        reverse=True,
    )[:top_k]
    return {k: entries[k] for k, _ in ranked}


# ---------------------------------------------------------------------------
# Tools: Memory management
# ---------------------------------------------------------------------------

def _word_jaccard(a: str, b: str) -> float:
    """Word-level Jaccard similarity between two strings."""
    ta = set(re.findall(r"[a-z0-9]+", a.lower()))
    tb = set(re.findall(r"[a-z0-9]+", b.lower()))
    union = ta | tb
    return len(ta & tb) / len(union) if union else 0.0


@mcp.tool()
def save_memory(project_path: str, key: str, content: str) -> str:
    """Save or update a memory entry for a project. Use short keys (e.g. 'stack', 'architecture')."""
    if key.startswith("_"):
        return "error: keys starting with '_' are reserved for system use"
    if not KEY_PATTERN.match(key):
        return "error: key must be 1-100 chars using only letters, digits, _ or -"
    content = redact_secrets(content)
    if len(content.encode("utf-8")) > MAX_ENTRY_KB * 1024:
        return f"error: content exceeds {MAX_ENTRY_KB}KB limit (set MCP_MAX_ENTRY_KB to override)"
    try:
        data = _read_memory(project_path)
        data[key] = content
        data[f"_updated_{key}"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _write_memory(project_path, data)

        existing = {k: v for k, v in data.items() if not k.startswith("_") and k != key}
        dupes = [(k, _word_jaccard(content, v)) for k, v in existing.items()]
        dupes = sorted([(k, s) for k, s in dupes if s >= MEMORY_SIMILARITY_THRESHOLD], key=lambda x: -x[1])

        result = f"saved: {key}"
        if dupes:
            dupe_str = ", ".join(f"'{k}' ({s:.2f})" for k, s in dupes)
            result += f" | warning: similar to existing key(s): {dupe_str}"
        return result
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def load_memory(project_path: str, query: str = "", top_k: int = 10) -> str:
    """Load saved memory for a project, optionally filtered by a TF-IDF query.

    query: space-separated words ranked by TF-IDF relevance; returns the top_k
    most relevant entries. Omit or pass "" to return all entries unranked.
    top_k: maximum number of entries to return when query is set (default 10).
    """
    try:
        data = _read_memory(project_path)
        if not any(not k.startswith("_") for k in data):
            return "no memory saved yet"
        level = max(0, min(2, int(data.get(COMPRESSION_KEY, DEFAULT_COMPRESSION))))
        if query.strip():
            words = re.findall(r"[a-z0-9]+", query.lower())
            system = {k: v for k, v in data.items() if k.startswith("_")}
            user_entries = {k: v for k, v in data.items() if not k.startswith("_")}
            ranked = _tfidf_rank(user_entries, words, top_k)
            if not ranked:
                return "no matching memory entries"
            data = {**system, **ranked}
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
        data.pop(f"_updated_{key}", None)
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
        summary = redact_secrets(summary)
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        if not tag_list:
            tag_list = history_store.extract_tags(summary)
        chunk_id = history_store.save_chunk(project_path, session_id[:8] if session_id else "", summary, tag_list)
        tag_str = ",".join(tag_list) if tag_list else "untagged"
        result = f"history saved: chunk {chunk_id} tags:[{tag_str}]"

        try:
            total = history_store.get_total_tokens(project_path)
            if total > HISTORY_MAX_TOKENS:
                history_store.summarise_oldest_chunks(project_path, HISTORY_SUMMARISE_N)
                result += " [auto-summarised: history over budget]"
        except Exception:
            pass

        return result
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
def backfill_bm25_text(project_path: str = "", batch_size: int = 100) -> str:
    """Write bm25_text (500-char BM25 corpus field) to chunks that lack it.

    Chunks saved before this field was introduced only have the 100-char preview
    and score with 5x less signal in BM25 retrieval. Run once after upgrading;
    repeat until 'remaining' = 0. Pass project_path="" to backfill all projects.
    """
    try:
        result = history_store.backfill_bm25_text(
            project=project_path or None,
            batch_size=batch_size,
        )
        if "reason" in result:
            return f"skipped: {result['reason']}"
        return f"backfilled: {result['backfilled']}, remaining: {result['remaining']}"
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def summarise_history(project_path: str, n: int = 10) -> str:
    """Collapse the n oldest history chunks into a single summary chunk.

    Reduces storage and retrieval noise by merging old context into a compact
    combined chunk with a type='summary' marker. Triggered automatically when
    total tokens exceed MCP_HISTORY_MAX_TOKENS (default 50000); call manually
    to compact immediately.
    """
    try:
        result = history_store.summarise_oldest_chunks(project_path, n)
        if "error" in result:
            return f"error: {result['error']}"
        if "reason" in result:
            return f"skipped: {result['reason']}"
        return (
            f"summarised: {result['summarised']} chunks into {result['new_chunk_id']}, "
            f"tokens_before: {result['tokens_before']}, tokens_after: {result['tokens_after']}"
        )
    except Exception as e:
        return f"error: {e}"


@mcp.tool()
def suggest_history(project_path: str, user_message: str, token_budget: int = 2000, diversity: float = 0.3) -> str:
    """Retrieve the most relevant history chunks for the current task.

    Selection logic:
      1. Vector search (if EMBED_PROVIDER configured) + BM25/tag scoring are run
         concurrently and merged with Reciprocal Rank Fusion (RRF, k=60).  Chunks
         appearing in both lists outrank chunks in only one.
      2. MMR re-ranking (diversity 0.0–1.0, default 0.3) penalises redundant
         chunks so the selected set covers more distinct topics.
      3. Anchor (most recent chunk) is guaranteed to be included for session
         continuity; it is BM25-scored and ranked like any other candidate.
      4. Remaining token budget is filled with the most recent unselected chunks.

    When no EMBED_PROVIDER is configured, only BM25+tag scoring is used and RRF
    is applied over the single list (preserving BM25 order).

    Call at session start with the user's first message instead of manually
    calling load_history + get_history_chunks.
    """
    try:
        recent_candidates = history_store.load_index(project_path, last_n=10)
        if not recent_candidates:
            return "no history yet"

        # Anchor: most recent chunk.  Guaranteed to be included for session
        # continuity, but scored by BM25 so it ranks by relevance — not fixed at 1.0.
        anchor = recent_candidates[0]
        anchor_id = anchor["id"]

        selected_ids: list = []
        score_map: dict = {}
        used = 0

        # Pool 1 (vector) and Pool 2 (BM25) are independent — run concurrently.
        # Vector search is I/O-bound (network + Atlas); BM25 is CPU-bound (pure).
        # GIL is released during I/O, so the embedding API call overlaps with BM25.
        vec_future = _suggest_executor.submit(
            history_store.search_by_vector, project_path, user_message, 10
        )
        bm25_future = _suggest_executor.submit(
            lambda: history_store.score_chunks(
                history_store.load_index(project_path, last_n=50), user_message
            )
        )
        vector_candidates = vec_future.result()
        scored = bm25_future.result()

        vector_ranked = [(e, e.get("score", 0.0), "vector") for e in vector_candidates]
        bm25_ranked = [(entry, sc, "bm25") for sc, entry in scored if sc > 0]

        # Merge both pools with Reciprocal Rank Fusion.  A chunk in both lists
        # gets a contribution from each rank, so it outranks a chunk in only one.
        active_lists = [lst for lst in [vector_ranked, bm25_ranked] if lst]
        merged = history_store.rrf_merge(active_lists) if active_lists else []

        # MMR re-ranking: penalise chunks similar to already-selected ones so the
        # result set spans more distinct topics (skipped when diversity=0.0).
        if diversity > 0.0 and len(merged) > 1:
            merged = history_store.mmr_rerank(merged, diversity)

        ranked = [(entry, rrf_score, source) for rrf_score, entry, source in merged]

        # Add ranked candidates (high score first) within budget.
        # The anchor entry is relabelled 'anchor' for output transparency.
        for entry, score, source in ranked:
            t = entry["stats"].get("tokens", 0)
            if used + t > token_budget:
                continue
            final_source = "anchor" if entry["id"] == anchor_id else source
            selected_ids.append(entry["id"])
            score_map[entry["id"]] = (final_source, score)
            used += t

        # Guarantee anchor inclusion: if BM25 didn't select it (very low score or
        # budget exhausted), force-add it so session continuity is always preserved.
        if anchor_id not in score_map:
            selected_ids.append(anchor_id)
            score_map[anchor_id] = ("anchor", 0.0)
            used += anchor["stats"].get("tokens", 0)

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

        # Present by relevance score (highest first) so the most relevant context
        # is seen first by both the LLM and retrieval benchmarks.
        chunks.sort(key=lambda c: -score_map.get(c["id"], ("?", 0.0))[1])

        lines = [
            f"=== Relevant History ({len(chunks)} chunks, {total_tokens} tokens) ===",
            "",
        ]
        for chunk in chunks:
            source, score = score_map.get(chunk["id"], ("?", 0.0))
            tag_str = ",".join(chunk.get("tags", [])) or "untagged"
            lines.append(f"[{chunk['id']}] {chunk['timestamp']} src={source} score={score:.2f} tags:[{tag_str}]")
            lines.append(chunk.get("dialogue", ""))
            lines.append("")
        return "\n".join(lines)
    except Exception as e:
        return f"error: {e}"


MAX_PROJECT_SCAN_DEPTH = 5


def _iter_project_dirs(root: pathlib.Path, max_depth: int, _current: int = 1):
    """Yield directories that contain MEMORY_FILE, up to max_depth levels deep."""
    try:
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            if (entry / MEMORY_FILE).exists():
                yield entry
            if _current < max_depth:
                yield from _iter_project_dirs(entry, max_depth, _current + 1)
    except PermissionError:
        pass


# ---------------------------------------------------------------------------
# Tools: Multi-project integration
# ---------------------------------------------------------------------------

@mcp.tool()
def list_projects(base_path: str, max_depth: int = 1) -> str:
    """List all projects under base_path that have saved memory, with key counts and last save time.

    max_depth: how many directory levels to scan (1 = direct children only, max 5).
    """
    try:
        root = _validate_project_path(base_path)
    except ValueError as e:
        return json.dumps({"error": str(e)})
    if not root.exists() or not root.is_dir():
        return json.dumps({"error": f"'{base_path}' is not a valid directory"})

    max_depth = max(1, min(max_depth, MAX_PROJECT_SCAN_DEPTH))
    projects = []
    for entry in _iter_project_dirs(root, max_depth):
        data = _load_json_safe(entry / MEMORY_FILE, {})
        keys = [k for k in data if not k.startswith("_")]
        last_save = history_store.get_latest_save(str(entry))
        projects.append({
            "path": str(entry),
            "name": entry.name,
            "key_count": len(keys),
            "last_save": last_save,
        })

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
def load_cross_project_memory(base_path: str, query_keys: str = "", max_depth: int = 1) -> str:
    """Load memory from all projects under base_path, optionally filtered by comma-separated keys.

    max_depth: how many directory levels to scan (1 = direct children only, max 5).
    """
    try:
        root = _validate_project_path(base_path)
    except ValueError as e:
        return f"error: {e}"
    if not root.exists() or not root.is_dir():
        return f"error: '{base_path}' is not a valid directory"

    max_depth = max(1, min(max_depth, MAX_PROJECT_SCAN_DEPTH))
    filter_keys = {k.strip() for k in query_keys.split(",") if k.strip()} if query_keys else set()

    sections = []
    for entry in _iter_project_dirs(root, max_depth):
        data = _load_json_safe(entry / MEMORY_FILE, {})
        level = max(0, min(2, int(data.get(COMPRESSION_KEY, DEFAULT_COMPRESSION))))
        entries = {k: v for k, v in data.items() if not k.startswith("_")}
        if filter_keys:
            entries = {k: v for k, v in entries.items() if k in filter_keys}
        if not entries:
            continue
        compressed = _compress_memory(entries, level)
        sections.append(f"=== {entry.name} ===\n{compressed}")

    return "\n\n".join(sections) if sections else "no projects with memory found"


@mcp.tool()
def search_across_projects(base_path: str, keyword: str, max_depth: int = 1) -> str:
    """Search memory values across all projects under base_path for a keyword (case-insensitive).

    max_depth: how many directory levels to scan (1 = direct children only, max 5).
    """
    try:
        root = _validate_project_path(base_path)
    except ValueError as e:
        return f"error: {e}"
    if not root.exists() or not root.is_dir():
        return f"error: '{base_path}' is not a valid directory"
    if not keyword.strip():
        return "error: keyword cannot be empty"

    max_depth = max(1, min(max_depth, MAX_PROJECT_SCAN_DEPTH))
    kw = keyword.lower()
    matches = []

    for entry in _iter_project_dirs(root, max_depth):
        data = _load_json_safe(entry / MEMORY_FILE, {})
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

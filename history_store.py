"""
Shared history storage layer used by history_hook.py and server.py.

MongoDB when MEMORY_MAP_MONGO_URI is set; JSON fallback (.mcp_history.json) otherwise.
Zero LLM calls — tags are extracted by local keyword matching.
"""

import json
import logging
import math
import os
import pathlib
import re
import datetime

logger = logging.getLogger(__name__)

import portalocker
from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).parent / ".env")

HISTORY_FILE = ".mcp_history.json"
MAX_JSON_CHUNKS = 100

# Per-tag keyword sets.  The dialogue is lowercased before matching, so all
# entries here must be lowercase.  Longer / more specific terms score higher
# because they're matched as substrings rather than whole words, giving file
# paths like "auth.py" or "docker-compose.yml" natural signal.
_TAG_KEYWORDS: dict = {
    "api-design":    ["endpoint", "route", "rest", "graphql", "openapi", "swagger",
                      "http", "request", "response", "fastapi", "flask", "express"],
    "architecture":  ["architecture", "diagram", "design", "pattern", "layer",
                      "service", "module", "component", "monolith", "microservice"],
    "auth":          ["auth", "login", "logout", "oauth", "jwt", "token", "session",
                      "password", "credential", "permission", "role", "bearer"],
    "bug-fix":       ["bug", "fix", "broken", "crash", "error", "exception",
                      "traceback", "fail", "incorrect", "wrong", "issue"],
    "configuration": ["config", "setting", "env", ".env", "yaml", "toml",
                      "dotenv", "environment variable", "secret", "flag"],
    "data-pipeline": ["pipeline", "etl", "spark", "pyspark", "airflow", "dbt",
                      "kafka", "stream", "batch", "ingest", "transform"],
    "database":      ["sql", "mongo", "postgres", "mysql", "sqlite", "redis",
                      "query", "schema", "migration", "index", "collection",
                      "table", "orm", "database", ".sql"],
    "debugging":     ["debug", "breakpoint", "pdb", "print(", "log", "trace",
                      "inspect", "investigate", "reproduce", "step through"],
    "deployment":    ["deploy", "docker", "kubernetes", "k8s", "helm", "ci",
                      "cd", "github action", "pipeline", "release", "build",
                      "dockerfile", "docker-compose", "terraform", "ansible"],
    "documentation": ["readme", "docstring", "comment", "doc", "wiki",
                      "changelog", "mkdocs", "sphinx", "markdown"],
    "feature":       ["add", "implement", "new feature", "feature", "build",
                      "create", "introduce", "support"],
    "memory":        ["memory", "mcp_memory", "save_memory", "load_memory",
                      "history", "checkpoint", "context"],
    "performance":   ["performance", "slow", "latency", "bottleneck", "profile",
                      "optimize", "cache", "speed", "benchmark", "timeout"],
    "refactor":      ["refactor", "restructure", "rename", "clean up", "simplify",
                      "reorganize", "extract", "move", "split"],
    "testing":       ["test", "pytest", "unittest", "mock", "assert", "coverage",
                      "fixture", "spec", "jest", "vitest", ".test.", "_test."],
    "tooling":       ["makefile", "script", "cli", "tool", "hook", "pre-commit",
                      "linter", "formatter", "black", "ruff", "eslint", "mypy"],
}

KNOWN_TAGS = sorted(_TAG_KEYWORDS.keys())

# Module-level MongoDB connection cache — initialised once per process.
_mongo_col = None
_mongo_init_done = False


def _get_collection():
    global _mongo_col, _mongo_init_done
    if _mongo_init_done:
        return _mongo_col
    _mongo_init_done = True

    uri = os.environ.get("MEMORY_MAP_MONGO_URI", "")
    if not uri:
        return None

    require_mongo = os.environ.get("MEMORY_MAP_REQUIRE_MONGO", "").lower() in ("1", "true", "yes")
    try:
        from pymongo import MongoClient  # optional dependency
        client = MongoClient(uri, serverSelectionTimeoutMS=1000)
        client.admin.command("ping")
        col = client["memory_map"]["history"]
        col.create_index([("project", 1), ("timestamp", -1)], background=True)
        col.create_index([("project", 1), ("tags", 1)], background=True)
        _mongo_col = col
        logger.info("memory_map: connected to MongoDB at %s", uri)
    except Exception as exc:
        if require_mongo:
            raise RuntimeError(
                f"memory_map: MongoDB required but unreachable at {uri!r}: {exc}"
            ) from exc
        logger.warning(
            "memory_map: MongoDB unreachable (%s) — falling back to JSON storage. "
            "Start MongoDB or remove MEMORY_MAP_MONGO_URI from .env to silence this.",
            exc,
        )

    return _mongo_col


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def compute_stats(dialogue: str) -> dict:
    chars = len(dialogue)
    return {"chars": chars, "tokens": chars // 4}


def extract_tags(dialogue: str) -> list:
    """Extract intent tags by keyword matching — zero LLM calls, zero API keys.

    Each tag accumulates a hit count from its keyword list; the top 5 tags by
    hit count are returned so the most relevant ones bubble up naturally.
    """
    text = dialogue.lower()
    scores: dict = {}
    for tag, keywords in _TAG_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in text)
        if hits:
            scores[tag] = hits
    # Sort by score descending, return top 5 tag names
    return [tag for tag, _ in sorted(scores.items(), key=lambda x: -x[1])][:5]


def save_chunk(project: str, session_id: str, dialogue: str, tags: list,
               group_id: str = None, part: int = None, total_parts: int = None) -> str:
    """Persist a history chunk. Returns the assigned chunk ID.

    group_id / part / total_parts are set only when a Q&A pair was split into
    multiple overlapping chunks so callers can reassemble them.
    """
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = compute_stats(dialogue)
    preview = dialogue[:100]

    col = _get_collection()
    if col is not None:
        doc = {
            "project": project,
            "session_id": session_id,
            "timestamp": ts,
            "dialogue": dialogue,
            "preview": preview,
            "tags": tags,
            "stats": stats,
        }
        if group_id is not None:
            doc["group_id"] = group_id
            doc["part"] = part
            doc["total_parts"] = total_parts
        result = col.insert_one(doc)
        return str(result.inserted_id)

    return _json_save(project, session_id, ts, dialogue, preview, tags, stats,
                      group_id=group_id, part=part, total_parts=total_parts)


def load_index(project: str, last_n: int = 20) -> list:
    """Return tag index records (no full dialogue)."""
    col = _get_collection()
    if col is not None:
        return _mongo_load_index(col, project, last_n)
    return _json_load_index(project, last_n)


def query_by_tags(project: str, tags: list, limit: int = 30) -> list:
    """Return index entries (no dialogue) for chunks matching any of the given tags.

    Sorted newest-first. Same dict shape as load_index entries.
    Returns [] immediately when tags is empty.
    """
    if not tags:
        return []
    col = _get_collection()
    if col is not None:
        return _mongo_query_by_tags(col, project, tags, limit)
    return _json_query_by_tags(project, tags, limit)


def get_chunks(project: str, ids: list) -> tuple:
    """Fetch full chunks by ID list. Returns (chunks, total_tokens)."""
    col = _get_collection()
    if col is not None:
        chunks = _mongo_get_chunks(col, ids)
    else:
        chunks = _json_get_chunks(project, ids)
    total_tokens = sum(c.get("stats", {}).get("tokens", 0) for c in chunks)
    return chunks, total_tokens


def get_latest_save(project: str) -> str:
    """Return ISO timestamp of the most recent chunk, or ''."""
    col = _get_collection()
    if col is not None:
        doc = col.find_one(
            {"project": project},
            {"timestamp": 1},
            sort=[("timestamp", -1)],
        )
        return doc.get("timestamp", "") if doc else ""
    data = _json_read(project)
    return data.get("_meta", {}).get("last_save", "")


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------

def score_chunks(index: list, user_message: str) -> list:
    """Score and sort index entries by relevance to user_message.

    Three signals combined:
      - tag-keyword match (0.5): checks _TAG_KEYWORDS values, not tag names
      - recency decay    (0.3): half-life 3 days — old noise fades out naturally
      - preview overlap  (0.2): word intersection between message and preview

    Returns list of (score: float, entry: dict) sorted by score descending.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    msg_lower = user_message.lower()
    msg_words = set(re.sub(r"[^\w\s]", " ", msg_lower).split())

    scored = []
    for entry in index:
        # Signal 1 — tag-keyword match
        tags = entry.get("tags", [])
        tag_hits = sum(
            1 for tag in tags
            if any(kw in msg_lower for kw in _TAG_KEYWORDS.get(tag, []))
        )
        tag_score = tag_hits / len(tags) if tags else 0.0

        # Signal 2 — recency decay (half-life = 3 days)
        try:
            ts = datetime.datetime.strptime(
                entry["timestamp"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=datetime.timezone.utc)
            age_days = (now - ts).total_seconds() / 86400
        except Exception:
            age_days = 999
        recency_score = math.exp(-age_days * math.log(2) / 3)

        # Signal 3 — preview word overlap
        preview_words = set(
            re.sub(r"[^\w\s]", " ", entry.get("preview", "").lower()).split()
        )
        preview_score = min(len(msg_words & preview_words) / 5.0, 1.0)

        combined = tag_score * 0.5 + recency_score * 0.3 + preview_score * 0.2
        scored.append((combined, entry))

    return sorted(scored, key=lambda x: -x[0])


# ---------------------------------------------------------------------------
# MongoDB backend
# ---------------------------------------------------------------------------

def _mongo_load_index(col, project: str, last_n: int) -> list:
    cursor = col.find(
        {"project": project},
        {"dialogue": 0},
    ).sort("timestamp", -1).limit(last_n)
    results = []
    for doc in cursor:
        results.append({
            "id": str(doc["_id"]),
            "timestamp": doc.get("timestamp", ""),
            "tags": doc.get("tags", []),
            "preview": doc.get("preview", ""),
            "stats": doc.get("stats", {}),
        })
    return results  # newest first (timestamp DESC)


def _mongo_query_by_tags(col, project: str, tags: list, limit: int) -> list:
    cursor = col.find(
        {"project": project, "tags": {"$in": tags}},
        {"dialogue": 0},
    ).sort("timestamp", -1).limit(limit)
    return [
        {
            "id": str(doc["_id"]),
            "timestamp": doc.get("timestamp", ""),
            "tags": doc.get("tags", []),
            "preview": doc.get("preview", ""),
            "stats": doc.get("stats", {}),
        }
        for doc in cursor
    ]


def _mongo_get_chunks(col, ids: list) -> list:
    from bson import ObjectId  # pymongo must be installed for MongoDB path
    oids = []
    for id_str in ids:
        try:
            oids.append(ObjectId(id_str))
        except Exception:
            pass
    docs = list(col.find({"_id": {"$in": oids}}).sort("timestamp", -1))
    return [{
        "id": str(doc["_id"]),
        "timestamp": doc.get("timestamp", ""),
        "session_id": doc.get("session_id", ""),
        "dialogue": doc.get("dialogue", ""),
        "tags": doc.get("tags", []),
        "stats": doc.get("stats", {}),
    } for doc in docs]


# ---------------------------------------------------------------------------
# JSON fallback backend
# ---------------------------------------------------------------------------

def _json_path(project: str) -> pathlib.Path:
    return pathlib.Path(project).resolve() / HISTORY_FILE


def _json_read(project: str) -> dict:
    p = _json_path(project)
    if not p.exists():
        return {"_meta": {}, "chunks": []}
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"_meta": {}, "chunks": []}


def _json_write(project: str, data: dict):
    p = _json_path(project)
    with open(p, "w", encoding="utf-8") as f:
        portalocker.lock(f, portalocker.LOCK_EX)
        json.dump(data, f, indent=2)
        portalocker.unlock(f)


def _json_save(project: str, session_id: str, ts: str,
               dialogue: str, preview: str, tags: list, stats: dict,
               group_id: str = None, part: int = None, total_parts: int = None) -> str:
    data = _json_read(project)
    chunks = data.get("chunks", [])

    try:
        next_id = str(int(chunks[-1].get("id", 0)) + 1) if chunks else "1"
    except (ValueError, TypeError):
        next_id = str(len(chunks) + 1)

    chunk = {
        "id": next_id,
        "session_id": session_id,
        "timestamp": ts,
        "dialogue": dialogue,
        "preview": preview,
        "tags": tags,
        "stats": stats,
    }
    if group_id is not None:
        chunk["group_id"] = group_id
        chunk["part"] = part
        chunk["total_parts"] = total_parts
    chunks.append(chunk)

    if len(chunks) > MAX_JSON_CHUNKS:
        chunks = chunks[-MAX_JSON_CHUNKS:]

    data["chunks"] = chunks
    data["_meta"]["last_save"] = ts
    _json_write(project, data)
    return next_id


def _json_load_index(project: str, last_n: int) -> list:
    chunks = list(reversed(_json_read(project).get("chunks", [])[-last_n:]))
    return [{
        "id": str(c.get("id", "")),
        "timestamp": c.get("timestamp", ""),
        "tags": c.get("tags", []),
        "preview": c.get("preview", c.get("dialogue", "")[:100]),
        "stats": c.get("stats", {}),
    } for c in chunks]


def _json_query_by_tags(project: str, tags: list, limit: int) -> list:
    tag_set = set(tags)
    all_chunks = _json_read(project).get("chunks", [])
    matches = [c for c in all_chunks if tag_set & set(c.get("tags", []))]
    matches = sorted(matches, key=lambda c: c.get("timestamp", ""), reverse=True)[:limit]
    return [
        {
            "id": str(c.get("id", "")),
            "timestamp": c.get("timestamp", ""),
            "tags": c.get("tags", []),
            "preview": c.get("preview", c.get("dialogue", "")[:100]),
            "stats": c.get("stats", {}),
        }
        for c in matches
    ]


def _json_get_chunks(project: str, ids: list) -> list:
    id_set = set(ids)
    result = []
    for c in _json_read(project).get("chunks", []):
        if str(c.get("id", "")) not in id_set:
            continue
        entry = {
            "id": str(c.get("id", "")),
            "timestamp": c.get("timestamp", ""),
            "session_id": c.get("session_id", ""),
            # support old 'summary' key for chunks saved before the redesign
            "dialogue": c.get("dialogue", c.get("summary", "")),
            "tags": c.get("tags", []),
            "stats": c.get("stats", {}),
        }
        if c.get("group_id") is not None:
            entry["group_id"] = c["group_id"]
            entry["part"] = c.get("part")
            entry["total_parts"] = c.get("total_parts")
        result.append(entry)
    return result

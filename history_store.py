"""
Shared history storage layer used by history_hook.py and server.py.

MongoDB only — MEMORY_MAP_MONGO_URI must be set in .env.
Zero LLM calls — tags are extracted by local keyword matching.
"""

import logging
import math
import os
import pathlib
import re
import datetime

logger = logging.getLogger(__name__)

from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).parent / ".env")

EMBED_MODEL = "text-embedding-3-small"
EMBED_DIMS = 1536

# "openai"  → generate embeddings via OpenAI, store on doc, search with queryVector
# "atlas"   → Atlas autoEmbed (Voyage-4), no client-side embedding, search with queryText
# ""        → no vector search, fall back to keyword tag matching
EMBED_PROVIDER = os.environ.get("MEMORY_MAP_EMBED_PROVIDER", "").lower()

ATLAS_AUTOEMBED_INDEX = "history_autoembed_index"
ATLAS_VECTOR_INDEX = "history_vector_index"


def _embed(text: str) -> list | None:
    """Return an OpenAI embedding vector, or None on failure. Only used when EMBED_PROVIDER=openai."""
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        logger.warning("memory_map: OPENAI_API_KEY not set — skipping embedding")
        return None
    try:
        from openai import OpenAI
        response = OpenAI(api_key=api_key).embeddings.create(model=EMBED_MODEL, input=text[:8000])
        return response.data[0].embedding
    except Exception as exc:
        logger.warning("memory_map: OpenAI embedding failed (%s) — chunk saved without vector", exc)
        return None

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
        raise RuntimeError(
            "memory_map: MEMORY_MAP_MONGO_URI is not set. "
            "Add it to your .env file to connect to MongoDB Atlas."
        )

    try:
        from pymongo import MongoClient
        client = MongoClient(uri, serverSelectionTimeoutMS=5000)
        client.admin.command("ping")
        col = client["memory_map"]["history"]
        col.create_index([("project", 1), ("timestamp", -1)], background=True)
        col.create_index([("project", 1), ("tags", 1)], background=True)
        _mongo_col = col
        logger.info("memory_map: connected to MongoDB at %s", uri)
    except Exception as exc:
        raise RuntimeError(
            f"memory_map: MongoDB unreachable at {uri!r}: {exc}"
        ) from exc

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
    return [tag for tag, _ in sorted(scores.items(), key=lambda x: -x[1])][:5]


def save_chunk(project: str, session_id: str, dialogue: str, tags: list,
               group_id: str = None, part: int = None, total_parts: int = None) -> str:
    """Persist a history chunk to MongoDB. Returns the inserted ObjectId as string."""
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = compute_stats(dialogue)
    doc = {
        "project": project,
        "session_id": session_id,
        "timestamp": ts,
        "dialogue": dialogue,
        "preview": dialogue[:100],
        "tags": tags,
        "stats": stats,
    }
    if EMBED_PROVIDER == "openai":
        embedding = _embed(dialogue)
        if embedding is not None:
            doc["embedding"] = embedding
    # atlas provider: autoEmbed generates the vector on insert — no client-side work needed
    if group_id is not None:
        doc["group_id"] = group_id
        doc["part"] = part
        doc["total_parts"] = total_parts
    result = _get_collection().insert_one(doc)
    return str(result.inserted_id)


def load_index(project: str, last_n: int = 20) -> list:
    """Return tag index records (no full dialogue), newest first."""
    cursor = _get_collection().find(
        {"project": project},
        {"dialogue": 0},
    ).sort("timestamp", -1).limit(last_n)
    return [{
        "id": str(doc["_id"]),
        "timestamp": doc.get("timestamp", ""),
        "tags": doc.get("tags", []),
        "preview": doc.get("preview", ""),
        "stats": doc.get("stats", {}),
    } for doc in cursor]


def query_by_tags(project: str, tags: list, limit: int = 30) -> list:
    """Return index entries for chunks matching any of the given tags, newest first."""
    if not tags:
        return []
    cursor = _get_collection().find(
        {"project": project, "tags": {"$in": tags}},
        {"dialogue": 0},
    ).sort("timestamp", -1).limit(limit)
    return [{
        "id": str(doc["_id"]),
        "timestamp": doc.get("timestamp", ""),
        "tags": doc.get("tags", []),
        "preview": doc.get("preview", ""),
        "stats": doc.get("stats", {}),
    } for doc in cursor]


def get_chunks(project: str, ids: list) -> tuple:
    """Fetch full chunks by ID list. Returns (chunks, total_tokens)."""
    from bson import ObjectId
    oids = []
    for id_str in ids:
        try:
            oids.append(ObjectId(id_str))
        except Exception:
            pass
    docs = list(_get_collection().find({"_id": {"$in": oids}}).sort("timestamp", -1))
    chunks = [{
        "id": str(doc["_id"]),
        "timestamp": doc.get("timestamp", ""),
        "session_id": doc.get("session_id", ""),
        "dialogue": doc.get("dialogue", ""),
        "tags": doc.get("tags", []),
        "stats": doc.get("stats", {}),
    } for doc in docs]
    total_tokens = sum(c.get("stats", {}).get("tokens", 0) for c in chunks)
    return chunks, total_tokens


def get_latest_save(project: str) -> str:
    """Return ISO timestamp of the most recent chunk, or ''."""
    doc = _get_collection().find_one(
        {"project": project},
        {"timestamp": 1},
        sort=[("timestamp", -1)],
    )
    return doc.get("timestamp", "") if doc else ""


def search_by_vector(project: str, query: str, limit: int = 5) -> list:
    """Return semantically similar chunks using Atlas Vector Search.

    Provider is selected by MEMORY_MAP_EMBED_PROVIDER:
      "openai" → client-side OpenAI embeddings + history_vector_index (queryVector)
      "atlas"  → Atlas autoEmbed (Voyage-4) + history_autoembed_index (queryText)
      ""       → returns [] so suggest_history falls back to tag matching
    """
    if EMBED_PROVIDER == "openai":
        embedding = _embed(query)
        if embedding is None:
            return []
        vector_search = {
            "index": ATLAS_VECTOR_INDEX,
            "path": "embedding",
            "queryVector": embedding,
            "numCandidates": limit * 10,
            "limit": limit,
            "filter": {"project": {"$eq": project}},
        }
        exclude_fields = {"dialogue": 0, "embedding": 0}

    elif EMBED_PROVIDER == "atlas":
        vector_search = {
            "index": ATLAS_AUTOEMBED_INDEX,
            "path": "dialogue",
            "queryText": query,
            "numCandidates": limit * 10,
            "limit": limit,
            "filter": {"project": {"$eq": project}},
        }
        exclude_fields = {"dialogue": 0}

    else:
        return []

    pipeline = [
        {"$vectorSearch": vector_search},
        {"$project": {**exclude_fields, "score": {"$meta": "vectorSearchScore"}}},
    ]
    docs = list(_get_collection().aggregate(pipeline))
    return [{
        "id": str(doc["_id"]),
        "timestamp": doc.get("timestamp", ""),
        "tags": doc.get("tags", []),
        "preview": doc.get("preview", ""),
        "stats": doc.get("stats", {}),
        "score": doc.get("score", 0.0),
    } for doc in docs]


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
        tags = entry.get("tags", [])
        tag_hits = sum(
            1 for tag in tags
            if any(kw in msg_lower for kw in _TAG_KEYWORDS.get(tag, []))
        )
        tag_score = tag_hits / len(tags) if tags else 0.0

        try:
            ts = datetime.datetime.strptime(
                entry["timestamp"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=datetime.timezone.utc)
            age_days = (now - ts).total_seconds() / 86400
        except Exception:
            age_days = 999
        recency_score = math.exp(-age_days * math.log(2) / 3)

        preview_words = set(
            re.sub(r"[^\w\s]", " ", entry.get("preview", "").lower()).split()
        )
        preview_score = min(len(msg_words & preview_words) / 5.0, 1.0)

        combined = tag_score * 0.5 + recency_score * 0.3 + preview_score * 0.2
        scored.append((combined, entry))

    return sorted(scored, key=lambda x: -x[0])

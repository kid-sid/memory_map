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
EMBED_MAX_CHARS = 30000  # ~7500 tokens, safely under the model's 8192-token limit
EMBED_RETRIES = 3

# Cosine score threshold below which a vector match is considered noise.
# Atlas vectorSearchScore is in [0, 1] where 0.5 == cosine similarity 0.
# 0.65 corresponds to cosine ~0.30 — empirical sweet spot for text-embedding-3-small.
MIN_VECTOR_SCORE = float(os.environ.get("MEMORY_MAP_MIN_VECTOR_SCORE", "0.65"))

# Queries shorter than this are treated as too vague for semantic search.
MIN_QUERY_CHARS = 4

# "openai"  → generate embeddings via OpenAI, store on doc, search with queryVector
# "atlas"   → Atlas autoEmbed (Voyage-4), no client-side embedding, search with queryText
# ""        → no vector search, fall back to keyword tag matching
EMBED_PROVIDER = os.environ.get("MEMORY_MAP_EMBED_PROVIDER", "").lower()

ATLAS_AUTOEMBED_INDEX = "history_autoembed_index"
ATLAS_VECTOR_INDEX = "history_vector_index"

_openai_client = None  # lazy singleton — created once, reused across calls and retries


def _embed(text: str) -> list | None:
    """Return an OpenAI embedding vector, or None on failure.

    Retries up to EMBED_RETRIES times with exponential backoff for transient
    failures (rate limits, network blips). Truncates to EMBED_MAX_CHARS so we
    never exceed the model's token limit.
    """
    global _openai_client

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        logger.warning("memory_map: OPENAI_API_KEY not set — skipping embedding")
        return None

    text = text[:EMBED_MAX_CHARS] if text else ""
    if not text.strip():
        return None

    if _openai_client is None:
        try:
            from openai import OpenAI
            _openai_client = OpenAI(api_key=api_key)
        except ImportError:
            logger.warning("memory_map: openai package not installed — skipping embedding")
            return None

    import time
    last_exc = None
    for attempt in range(EMBED_RETRIES):
        try:
            response = _openai_client.embeddings.create(model=EMBED_MODEL, input=text)
            return response.data[0].embedding
        except Exception as exc:
            last_exc = exc
            if attempt < EMBED_RETRIES - 1:
                wait = 2 ** attempt
                logger.warning("memory_map: embedding attempt %d failed (%s), retrying in %ds",
                               attempt + 1, exc, wait)
                time.sleep(wait)

    logger.error("memory_map: OpenAI embedding failed after %d attempts (%s) — chunk saved without vector",
                 EMBED_RETRIES, last_exc)
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
        logger.debug("memory_map: MEMORY_MAP_MONGO_URI not set — MongoDB history unavailable")
        return None

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
               group_id: str = None, part: int = None, total_parts: int = None,
               embed: bool = True) -> str:
    """Persist a history chunk to MongoDB. Returns the inserted ObjectId as string.

    embed=False skips the client-side OpenAI embedding call (useful for hooks
    that must return quickly; run backfill_history_embeddings later to catch up).
    Atlas autoEmbed is always server-side and unaffected by this flag.
    """
    col = _get_collection()
    if col is None:
        raise RuntimeError("memory_map: MEMORY_MAP_MONGO_URI is not set — cannot save history")
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stats = compute_stats(dialogue)
    doc = {
        "project": project,
        "session_id": session_id,
        "timestamp": ts,
        "dialogue": dialogue,
        "preview": dialogue[:100],
        "bm25_text": dialogue[:500],
        "tags": tags,
        "stats": stats,
    }
    if embed and EMBED_PROVIDER == "openai":
        embedding = _embed(dialogue)
        if embedding is not None:
            doc["embedding"] = embedding
    # atlas provider: autoEmbed generates the vector on insert — no client-side work needed
    if group_id is not None:
        doc["group_id"] = group_id
        doc["part"] = part
        doc["total_parts"] = total_parts
    result = col.insert_one(doc)
    return str(result.inserted_id)


def load_index(project: str, last_n: int = 20) -> list:
    """Return tag index records (no full dialogue), newest first."""
    col = _get_collection()
    if col is None:
        return []
    cursor = col.find(
        {"project": project},
        {"dialogue": 0},
    ).sort([("timestamp", -1), ("_id", -1)]).limit(last_n)
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
    col = _get_collection()
    if col is None:
        return []
    cursor = col.find(
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
    col = _get_collection()
    if col is None:
        return [], 0
    from bson import ObjectId
    oids = []
    for id_str in ids:
        try:
            oids.append(ObjectId(id_str))
        except Exception:
            pass
    docs = list(col.find({"_id": {"$in": oids}, "project": project}).sort("timestamp", -1))
    chunks = []
    for doc in docs:
        entry = {
            "id": str(doc["_id"]),
            "timestamp": doc.get("timestamp", ""),
            "session_id": doc.get("session_id", ""),
            "dialogue": doc.get("dialogue", ""),
            "tags": doc.get("tags", []),
            "stats": doc.get("stats", {}),
        }
        if "group_id" in doc:
            entry["group_id"] = doc["group_id"]
            entry["part"] = doc.get("part")
            entry["total_parts"] = doc.get("total_parts")
        chunks.append(entry)
    total_tokens = sum(c.get("stats", {}).get("tokens", 0) for c in chunks)
    return chunks, total_tokens


def get_latest_save(project: str) -> str:
    """Return ISO timestamp of the most recent chunk, or ''."""
    col = _get_collection()
    if col is None:
        return ""
    doc = col.find_one(
        {"project": project},
        {"timestamp": 1},
        sort=[("timestamp", -1)],
    )
    return doc.get("timestamp", "") if doc else ""


def search_by_vector(project: str, query: str, limit: int = 10,
                     min_score: float = None) -> list:
    """Return semantically similar chunks using Atlas Vector Search.

    Provider is selected by MEMORY_MAP_EMBED_PROVIDER:
      "openai" → client-side OpenAI embeddings + history_vector_index (queryVector)
      "atlas"  → Atlas autoEmbed (Voyage-4) + history_autoembed_index (queryText)
      ""       → returns [] so suggest_history falls back to tag matching

    Returns at most `limit` chunks where Atlas vectorSearchScore >= min_score.
    Empty/very short queries return [] immediately.
    """
    if not query or len(query.strip()) < MIN_QUERY_CHARS:
        return []
    if min_score is None:
        min_score = MIN_VECTOR_SCORE

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
    col = _get_collection()
    if col is None:
        return []
    try:
        docs = list(col.aggregate(pipeline))
    except Exception as exc:
        logger.error("memory_map: vector search failed (%s) — falling back", exc)
        return []

    results = []
    skipped = 0
    for doc in docs:
        score = doc.get("score", 0.0)
        if score < min_score:
            skipped += 1
            continue
        results.append({
            "id": str(doc["_id"]),
            "timestamp": doc.get("timestamp", ""),
            "tags": doc.get("tags", []),
            "preview": doc.get("preview", ""),
            "stats": doc.get("stats", {}),
            "score": score,
        })

    if skipped:
        logger.info("memory_map: vector search returned %d chunks, %d below min_score=%.2f",
                    len(results), skipped, min_score)
    return results


def backfill_embeddings(project: str = None, batch_size: int = 20) -> dict:
    """Embed and update chunks that lack an `embedding` field.

    Useful after enabling vector search to make pre-existing chunks searchable.
    If project is None, backfills across all projects. Returns counts.
    Only meaningful when EMBED_PROVIDER=openai; atlas autoEmbed handles its own.
    """
    if EMBED_PROVIDER != "openai":
        return {"backfilled": 0, "skipped": 0, "reason": f"backfill only applies to openai provider, current={EMBED_PROVIDER!r}"}

    col = _get_collection()
    if col is None:
        return {"backfilled": 0, "skipped": 0, "reason": "MongoDB not configured"}
    query = {"embedding": {"$exists": False}}
    if project:
        query["project"] = project

    cursor = col.find(query, {"_id": 1, "dialogue": 1}).limit(batch_size)
    docs = list(cursor)
    if not docs:
        return {"backfilled": 0, "skipped": 0, "remaining": 0}

    backfilled = 0
    failed = 0
    for doc in docs:
        dialogue = doc.get("dialogue", "") or ""
        if not dialogue.strip():
            failed += 1
            continue
        embedding = _embed(dialogue)
        if embedding is None:
            failed += 1
            continue
        col.update_one({"_id": doc["_id"]}, {"$set": {"embedding": embedding}})
        backfilled += 1

    remaining = col.count_documents(query)
    return {"backfilled": backfilled, "failed": failed, "remaining": remaining}


def backfill_bm25_text(project: str = None, batch_size: int = 100) -> dict:
    """Write bm25_text (first 500 chars of dialogue) to chunks that lack the field.

    bm25_text was added in a later release; existing chunks only have the 100-char
    preview and score with 5x less signal.  Run this once after upgrading to bring
    all chunks up to the current schema.  If project is None, backfills globally.
    """
    col = _get_collection()
    if col is None:
        return {"backfilled": 0, "remaining": 0, "reason": "MongoDB not configured"}

    query: dict = {"bm25_text": {"$exists": False}}
    if project:
        query["project"] = project

    cursor = col.find(query, {"_id": 1, "dialogue": 1}).limit(batch_size)
    docs = list(cursor)
    if not docs:
        return {"backfilled": 0, "remaining": 0}

    ops = []
    from pymongo import UpdateOne
    for doc in docs:
        bm25_text = (doc.get("dialogue") or "")[:500]
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": {"bm25_text": bm25_text}}))

    if ops:
        col.bulk_write(ops, ordered=False)

    remaining = col.count_documents(query)
    return {"backfilled": len(ops), "remaining": remaining}


# ---------------------------------------------------------------------------
# Relevance scoring
# ---------------------------------------------------------------------------

def _bm25_scores(query_terms: list, corpus: list, k1: float = 1.5, b: float = 0.75) -> list:
    """Okapi BM25 scores for query_terms against each document in corpus.

    IDF uses Lucene-style smoothing: log((N - df + 0.5) / (df + 0.5) + 1).
    IDF is computed over the candidate set only (same as mempalace searcher.py).
    Returns a list of float scores, one per document.
    """
    N = len(corpus)
    if N == 0 or not query_terms:
        return [0.0] * N

    tokenized = [re.sub(r"[^\w\s]", " ", doc.lower()).split() for doc in corpus]
    avg_len = sum(len(d) for d in tokenized) / N

    df: dict = {}
    for terms in tokenized:
        for t in set(terms):
            df[t] = df.get(t, 0) + 1

    scores = []
    for doc_terms in tokenized:
        doc_len = len(doc_terms)
        tf_map: dict = {}
        for t in doc_terms:
            tf_map[t] = tf_map.get(t, 0) + 1

        score = 0.0
        for term in query_terms:
            tf = tf_map.get(term, 0)
            if tf == 0:
                continue
            idf = math.log((N - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5) + 1)
            tf_norm = (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * doc_len / avg_len))
            score += idf * tf_norm
        scores.append(score)

    return scores


def _expand_query(msg_lower: str) -> list:
    """Expand query terms using TAG_KEYWORDS synonyms.

    For every tag whose keywords appear in the message, add all single-word
    keywords from that tag to the query term set.  This lets "broken auth"
    match chunks that mention "jwt" or "session" even if those words aren't
    in the original message.
    """
    base_terms = set(re.sub(r"[^\w\s]", " ", msg_lower).split())
    expanded = set(base_terms)
    for tag, keywords in _TAG_KEYWORDS.items():
        if any(kw in msg_lower for kw in keywords):
            expanded.update(kw for kw in keywords if " " not in kw)
    return list(expanded)


def score_chunks(index: list, user_message: str) -> list:
    """Score and sort index entries by relevance to user_message.

    Signals (adapted from mempalace hybrid_v2 approach):
      - tag-keyword match (0.50): checks _TAG_KEYWORDS values, not tag names
      - BM25 text score (0.30): Okapi BM25 over bm25_text (500 chars, falls back to preview for older chunks)
      - recency decay     (0.20): half-life 3 days — old noise fades out naturally

    Returns list of (score: float, entry: dict) sorted by score descending.
    """
    if not index:
        return []

    now = datetime.datetime.now(datetime.timezone.utc)
    msg_lower = user_message.lower()

    # Expanded query terms for BM25 (adds tag synonyms)
    query_terms = _expand_query(msg_lower)

    # BM25 over bm25_text (500 chars); fall back to preview for older chunks
    corpus = [entry.get("bm25_text", entry.get("preview", "")) for entry in index]
    bm25_raw = _bm25_scores(query_terms, corpus)
    max_bm25 = max(bm25_raw) if bm25_raw else 1.0

    scored = []
    for i, entry in enumerate(index):
        # Tag-keyword signal
        tags = entry.get("tags", [])
        tag_hits = sum(
            1 for tag in tags
            if any(kw in msg_lower for kw in _TAG_KEYWORDS.get(tag, []))
        )
        tag_score = min(tag_hits, 1.0)

        # Recency signal
        try:
            ts = datetime.datetime.strptime(
                entry["timestamp"], "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=datetime.timezone.utc)
            age_days = (now - ts).total_seconds() / 86400
        except Exception:
            age_days = 999
        recency_score = math.exp(-age_days * math.log(2) / 3)

        # BM25 signal (normalised by max in candidate set)
        bm25_score = bm25_raw[i] / max_bm25 if max_bm25 > 0 else 0.0

        combined = tag_score * 0.5 + bm25_score * 0.3 + recency_score * 0.2
        scored.append((combined, entry))

    return sorted(scored, key=lambda x: -x[0])


def rrf_merge(ranked_lists: list, k: int = 60) -> list:
    """Reciprocal Rank Fusion over multiple ranked entry lists.

    Each input list is a sequence of (entry, score, source) tuples, best-first.
    RRF score for chunk i = sum(1 / (k + rank_i)) across all lists where it
    appears (ranks are 1-indexed).  k=60 is the standard constant.

    A chunk appearing in both a vector list and a BM25 list receives contributions
    from both and will outrank a chunk that appears in only one — which is the
    hybrid retrieval guarantee.

    Returns a single list of (rrf_score, entry, source_label) sorted desc.
    source_label joins source names from every contributing list, e.g. "bm25+vector".
    """
    rrf_scores: dict = {}
    entries: dict = {}
    sources: dict = {}

    for ranked in ranked_lists:
        for rank, (entry, _score, source) in enumerate(ranked, start=1):
            eid = entry["id"]
            rrf_scores[eid] = rrf_scores.get(eid, 0.0) + 1.0 / (k + rank)
            entries[eid] = entry
            sources.setdefault(eid, set()).add(source)

    result = []
    for eid, rrf_score in sorted(rrf_scores.items(), key=lambda x: -x[1]):
        source_label = "+".join(sorted(sources[eid]))
        result.append((rrf_score, entries[eid], source_label))
    return result

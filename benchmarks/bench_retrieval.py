#!/usr/bin/env python3
"""
Retrieval benchmark for memory_map suggest_history.

Measures Precision@k, MRR, and latency against a fixed test set of
(query, relevant_chunk_tags) pairs seeded into a temporary MongoDB project.

Usage:
    python benchmarks/bench_retrieval.py
    python benchmarks/bench_retrieval.py --k 5 --budget 4000 --verbose
"""

import argparse
import math
import pathlib
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import history_store
from server import suggest_history

# ---------------------------------------------------------------------------
# Seed data — chunks with known content and tags
# ---------------------------------------------------------------------------

SEED_CHUNKS = [
    {
        "id": "auth-jwt",
        "dialogue": (
            "user: The JWT token keeps expiring too early. Users are being logged out after 5 minutes.\n"
            "assistant: Increased JWT expiry from 300s to 3600s in auth.py. Also added a refresh token "
            "endpoint at POST /auth/refresh so clients can silently renew without re-login."
        ),
        "tags": ["auth"],
    },
    {
        "id": "postgres-migration",
        "dialogue": (
            "user: The postgres migration failed — column already exists error on ALTER TABLE.\n"
            "assistant: Added IF NOT EXISTS guard to the ALTER TABLE statement in migration 0042. "
            "Re-ran successfully. The column was added by a hotfix last week outside the migration system."
        ),
        "tags": ["database"],
    },
    {
        "id": "docker-deploy",
        "dialogue": (
            "user: The docker build fails on CI but works locally. COPY command can't find requirements.txt.\n"
            "assistant: Fixed COPY path from ./requirements.txt to ./backend/requirements.txt in the "
            "Dockerfile. The repo was restructured last sprint and the Dockerfile wasn't updated."
        ),
        "tags": ["deployment"],
    },
    {
        "id": "pytest-fixture",
        "dialogue": (
            "user: The pytest fixture for the database connection is leaking connections across tests.\n"
            "assistant: Added explicit conn.close() in the fixture teardown using yield + finally. "
            "Also switched from function scope to session scope to share one connection per test run."
        ),
        "tags": ["testing", "database"],
    },
    {
        "id": "fastapi-endpoint",
        "dialogue": (
            "user: Need a GET /healthz endpoint that returns service version and uptime.\n"
            "assistant: Added to server.py. Returns JSON with status, version from settings.VERSION, "
            "and uptime_seconds computed from process start time. No auth required."
        ),
        "tags": ["api-design"],
    },
    {
        "id": "redis-cache",
        "dialogue": (
            "user: The product listing endpoint is slow — 2s average response time.\n"
            "assistant: Added Redis cache with 60s TTL on GET /products. Response time dropped to 40ms "
            "on cache hits. Cache is invalidated on POST/PUT/DELETE to /products."
        ),
        "tags": ["performance", "database"],
    },
    {
        "id": "env-config",
        "dialogue": (
            "user: The app crashes on startup because DATABASE_URL is not set in production.\n"
            "assistant: Added validation in settings.py that checks required env vars at startup and "
            "raises a clear ConfigurationError listing the missing variables."
        ),
        "tags": ["configuration", "bug-fix"],
    },
    {
        "id": "refactor-auth",
        "dialogue": (
            "user: The auth module is 800 lines — let's split it up.\n"
            "assistant: Split into auth/tokens.py (JWT logic), auth/oauth.py (OAuth flows), and "
            "auth/middleware.py (request guards). Updated all imports. Tests still pass."
        ),
        "tags": ["auth", "refactor"],
    },
    {
        "id": "mongo-index",
        "dialogue": (
            "user: The history query is slow on large collections — taking 3s.\n"
            "assistant: Added compound index on (project, timestamp) in MongoDB. Query time dropped "
            "from 3s to 8ms on a 100k document collection."
        ),
        "tags": ["database", "performance"],
    },
    {
        "id": "ci-pipeline",
        "dialogue": (
            "user: The GitHub Actions pipeline is running tests on every push including docs changes.\n"
            "assistant: Added path filters to the workflow — tests only run when .py files or "
            "requirements.txt change. Docs-only pushes now skip the test job entirely."
        ),
        "tags": ["deployment", "tooling"],
    },
]

# ---------------------------------------------------------------------------
# Evaluation queries — (message, set of chunk IDs that are relevant)
# ---------------------------------------------------------------------------

EVAL_QUERIES = [
    {
        "query": "fix the login and JWT token expiry issue",
        "relevant": {"auth-jwt", "refactor-auth"},
        "description": "auth / JWT",
    },
    {
        "query": "postgres database migration failing",
        "relevant": {"postgres-migration", "pytest-fixture", "mongo-index"},
        "description": "database",
    },
    {
        "query": "docker build broken on CI pipeline",
        "relevant": {"docker-deploy", "ci-pipeline"},
        "description": "deployment / CI",
    },
    {
        "query": "slow API response time performance",
        "relevant": {"redis-cache", "mongo-index"},
        "description": "performance",
    },
    {
        "query": "environment variable config missing in production",
        "relevant": {"env-config"},
        "description": "configuration",
    },
    {
        "query": "pytest test fixture database connection",
        "relevant": {"pytest-fixture"},
        "description": "testing",
    },
    {
        "query": "new REST endpoint healthcheck",
        "relevant": {"fastapi-endpoint"},
        "description": "API design",
    },
    {
        "query": "refactor split large module into smaller files",
        "relevant": {"refactor-auth"},
        "description": "refactor",
    },
]


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def precision_at_k(retrieved_ids: list, relevant: set, k: int) -> float:
    top_k = retrieved_ids[:k]
    hits = sum(1 for id_ in top_k if id_ in relevant)
    return hits / k if k else 0.0


def recall_at_k(retrieved_ids: list, relevant: set, k: int) -> float:
    top_k = retrieved_ids[:k]
    hits = sum(1 for id_ in top_k if id_ in relevant)
    return hits / len(relevant) if relevant else 0.0


def reciprocal_rank(retrieved_ids: list, relevant: set) -> float:
    for i, id_ in enumerate(retrieved_ids, 1):
        if id_ in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(retrieved_ids: list, relevant: set, k: int) -> float:
    dcg = sum(
        1.0 / math.log2(i + 2)
        for i, id_ in enumerate(retrieved_ids[:k])
        if id_ in relevant
    )
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


# ---------------------------------------------------------------------------
# Parse suggest_history output → ordered list of chunk IDs
# ---------------------------------------------------------------------------

def parse_retrieved_ids(output: str) -> list:
    ids = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("[") and "]" in line:
            chunk_id = line[1: line.index("]")]
            if chunk_id and chunk_id != "?":
                ids.append(chunk_id)
    return ids


# ---------------------------------------------------------------------------
# Main benchmark
# ---------------------------------------------------------------------------

def run_benchmark(k: int, token_budget: int, verbose: bool):
    col = history_store._get_collection()
    if col is None:
        print("ERROR: MEMORY_MAP_MONGO_URI is not set — cannot run benchmark.")
        sys.exit(1)

    project = f"__benchmark_{uuid.uuid4().hex[:8]}__"
    print(f"Benchmark project: {project}")
    print(f"Seeding {len(SEED_CHUNKS)} chunks...")

    # Map from our human-readable IDs to MongoDB ObjectId strings
    id_map: dict[str, str] = {}
    for chunk in SEED_CHUNKS:
        mongo_id = history_store.save_chunk(
            project, "bench", chunk["dialogue"], chunk["tags"]
        )
        id_map[chunk["id"]] = mongo_id

    print(f"Running {len(EVAL_QUERIES)} queries with k={k}, budget={token_budget} tokens...\n")

    results = []
    latencies = []

    for q in EVAL_QUERIES:
        relevant_mongo_ids = {id_map[id_] for id_ in q["relevant"] if id_ in id_map}

        t0 = time.perf_counter()
        output = suggest_history(project, q["query"], token_budget=token_budget)
        latency_ms = (time.perf_counter() - t0) * 1000

        retrieved = parse_retrieved_ids(output)
        latencies.append(latency_ms)

        p_at_k = precision_at_k(retrieved, relevant_mongo_ids, k)
        r_at_k = recall_at_k(retrieved, relevant_mongo_ids, k)
        rr = reciprocal_rank(retrieved, relevant_mongo_ids)
        ndcg = ndcg_at_k(retrieved, relevant_mongo_ids, k)

        results.append({"p": p_at_k, "r": r_at_k, "rr": rr, "ndcg": ndcg})

        if verbose:
            hit = any(id_ in relevant_mongo_ids for id_ in retrieved[:k])
            status = "HIT " if hit else "MISS"
            print(
                f"  [{status}] {q['description']:<20} "
                f"P@{k}={p_at_k:.2f}  R@{k}={r_at_k:.2f}  "
                f"RR={rr:.2f}  nDCG@{k}={ndcg:.2f}  "
                f"{latency_ms:.0f}ms"
            )
            print(f"         query   : {q['query']}")
            print(f"         retrieved: {retrieved[:k]}")
            print(f"         relevant : {list(q['relevant'])}")
            print()

    # Aggregate
    n = len(results)
    mean_p    = sum(r["p"]    for r in results) / n
    mean_r    = sum(r["r"]    for r in results) / n
    mrr       = sum(r["rr"]   for r in results) / n
    mean_ndcg = sum(r["ndcg"] for r in results) / n
    mean_lat  = sum(latencies) / n
    p95_lat   = sorted(latencies)[int(0.95 * n)]

    print("=" * 60)
    print(f"Results  (k={k}, {n} queries)")
    print("=" * 60)
    print(f"  Precision@{k}   : {mean_p:.3f}")
    print(f"  Recall@{k}      : {mean_r:.3f}")
    print(f"  MRR            : {mrr:.3f}")
    print(f"  nDCG@{k}        : {mean_ndcg:.3f}")
    print(f"  Latency (mean) : {mean_lat:.0f}ms")
    print(f"  Latency (p95)  : {p95_lat:.0f}ms")
    print("=" * 60)

    # Cleanup
    col.delete_many({"project": project})
    print(f"\nCleaned up {len(SEED_CHUNKS)} benchmark chunks.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="memory_map retrieval benchmark")
    parser.add_argument("--k", type=int, default=3, help="Precision/Recall/nDCG cutoff (default: 3)")
    parser.add_argument("--budget", type=int, default=2000, help="Token budget for suggest_history (default: 2000)")
    parser.add_argument("--verbose", action="store_true", help="Print per-query breakdown")
    args = parser.parse_args()

    run_benchmark(k=args.k, token_budget=args.budget, verbose=args.verbose)

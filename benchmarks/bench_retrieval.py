#!/usr/bin/env python3
"""
Retrieval benchmark for memory_map suggest_history.

Measures Recall@k (primary), Precision@k, MRR, nDCG@k, and latency against
a fixed test set of (query, relevant_chunk_ids) pairs seeded into a temporary
MongoDB project.  Results are saved to benchmarks/results/<timestamp>.json
for tracking improvement across commits.

Inspired by mempalace LongMemEval benchmark approach:
  - Recall@{1,3,5} as primary metrics (not precision)
  - Per-query breakdown with pass/fail
  - JSON result files committed alongside code changes

Usage:
    python benchmarks/bench_retrieval.py
    python benchmarks/bench_retrieval.py --k 5 --budget 4000 --verbose
"""

import argparse
import datetime
import json
import math
import pathlib
import re
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

import history_store
from server import suggest_history

RESULTS_DIR = pathlib.Path(__file__).parent / "results"

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
# Evaluation queries — (message, set of human-readable chunk IDs that are relevant)
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

def recall_at_k(retrieved: list, relevant: set, k: int) -> float:
    hits = sum(1 for id_ in retrieved[:k] if id_ in relevant)
    return hits / len(relevant) if relevant else 0.0


def precision_at_k(retrieved: list, relevant: set, k: int) -> float:
    hits = sum(1 for id_ in retrieved[:k] if id_ in relevant)
    return hits / k if k else 0.0


def reciprocal_rank(retrieved: list, relevant: set) -> float:
    for i, id_ in enumerate(retrieved, 1):
        if id_ in relevant:
            return 1.0 / i
    return 0.0


def ndcg_at_k(retrieved: list, relevant: set, k: int) -> float:
    dcg = sum(
        1.0 / math.log2(i + 2)
        for i, id_ in enumerate(retrieved[:k])
        if id_ in relevant
    )
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevant), k)))
    return dcg / ideal if ideal else 0.0


# ---------------------------------------------------------------------------
# Parse suggest_history output → ordered list of MongoDB ObjectId strings
# ---------------------------------------------------------------------------

_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{24}$")

def parse_retrieved_ids(output: str) -> list:
    """Extract chunk IDs from suggest_history output in presentation order."""
    ids = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("[") and "]" in line:
            candidate = line[1: line.index("]")]
            if _OBJECT_ID_RE.match(candidate):
                ids.append(candidate)
    return ids


# ---------------------------------------------------------------------------
# Regression comparison
# ---------------------------------------------------------------------------

def _load_latest_result(results_dir: pathlib.Path, exclude: str = "") -> tuple:
    """Return (path, data) of the most recent result JSON, skipping `exclude` filename."""
    for candidate in sorted(results_dir.glob("*.json"), reverse=True):
        if candidate.name == exclude:
            continue
        try:
            return candidate, json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None, None


def _compare_results(baseline_path: pathlib.Path, baseline: dict,
                     current: dict, threshold: float) -> bool:
    """Print a metric delta table. Returns True if any primary metric regressed."""
    b_agg = baseline["aggregate"]
    c_agg = current["aggregate"]
    k = current["config"]["k"]
    primary = {"recall@1", "recall@3", f"recall@{k}"}

    print(f"\nDelta vs {baseline_path.name}")
    print("-" * 60)

    has_regression = False
    score_metrics = [m for m in sorted(c_agg) if not m.startswith("latency")]
    for metric in score_metrics:
        if metric not in b_agg:
            continue
        b_val, c_val = b_agg[metric], c_agg[metric]
        delta = c_val - b_val
        flag = ""
        if metric in primary and delta < -threshold:
            flag = "  *** REGRESSION ***"
            has_regression = True
        print(f"  {metric:<16}: {b_val:.3f} -> {c_val:.3f}  ({delta:+.3f}){flag}")

    for metric in ["latency_mean_ms", "latency_p95_ms"]:
        if metric in b_agg and metric in c_agg:
            b_val, c_val = b_agg[metric], c_agg[metric]
            print(f"  {metric:<16}: {b_val:.0f}ms -> {c_val:.0f}ms  ({c_val - b_val:+.0f}ms)")

    print("-" * 60)
    if has_regression:
        print(f"REGRESSION DETECTED — primary metric(s) dropped > {threshold:.0%}")
    else:
        print("No regression detected.")
    return has_regression


# ---------------------------------------------------------------------------
# Git metadata for result files
# ---------------------------------------------------------------------------

def _git_info() -> dict:
    def _run(cmd):
        try:
            return subprocess.check_output(cmd, cwd=str(pathlib.Path(__file__).parent.parent),
                                           text=True, stderr=subprocess.DEVNULL).strip()
        except Exception:
            return ""
    return {
        "commit": _run(["git", "rev-parse", "--short", "HEAD"]),
        "branch": _run(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "message": _run(["git", "log", "-1", "--format=%s"]),
    }


# ---------------------------------------------------------------------------
# Main benchmark
# ---------------------------------------------------------------------------

def run_benchmark(k: int, token_budget: int, verbose: bool, index_wait: int = 0,
                  compare: bool = False, threshold: float = 0.05):
    col = history_store._get_collection()
    if col is None:
        print("ERROR: MEMORY_MAP_MONGO_URI is not set — cannot run benchmark.")
        sys.exit(1)

    project = f"__benchmark_{uuid.uuid4().hex[:8]}__"
    print(f"Benchmark project : {project}")
    embed_provider = history_store.EMBED_PROVIDER
    print(f"Seeding {len(SEED_CHUNKS)} chunks into MongoDB...  (embed_provider={embed_provider!r})")

    id_map: dict[str, str] = {}
    for chunk in SEED_CHUNKS:
        # embed=False — we batch-embed all chunks after seeding (one API call instead of N).
        mongo_id = history_store.save_chunk(
            project, "bench", chunk["dialogue"], chunk["tags"], embed=False
        )
        id_map[chunk["id"]] = mongo_id

    reverse_map = {v: k for k, v in id_map.items()}

    # Batch-embed all seeded chunks in a single API call, then wait for Atlas to index.
    if embed_provider in ("openai", "local"):
        print(f"Embedding {len(SEED_CHUNKS)} chunks via {embed_provider!r} provider (batch)...")
        result = history_store.backfill_embeddings(project=project, batch_size=len(SEED_CHUNKS) + 1)
        print(f"  backfill: {result}")
        auto_wait = index_wait if index_wait > 0 else (20 if embed_provider == "openai" else 2)
        print(f"Waiting {auto_wait}s for Atlas vector index to sync...")
        time.sleep(auto_wait)

    print(f"Running {len(EVAL_QUERIES)} queries  k={k}  budget={token_budget} tokens\n")

    per_query = []
    latencies = []

    for q in EVAL_QUERIES:
        relevant_mongo = {id_map[i] for i in q["relevant"] if i in id_map}

        t0 = time.perf_counter()
        output = suggest_history(project, q["query"], token_budget=token_budget)
        latency_ms = (time.perf_counter() - t0) * 1000

        retrieved = parse_retrieved_ids(output)
        latencies.append(latency_ms)

        r1  = recall_at_k(retrieved, relevant_mongo, 1)
        r3  = recall_at_k(retrieved, relevant_mongo, 3)
        r5  = recall_at_k(retrieved, relevant_mongo, k)
        p_k = precision_at_k(retrieved, relevant_mongo, k)
        rr  = reciprocal_rank(retrieved, relevant_mongo)
        ndcg = ndcg_at_k(retrieved, relevant_mongo, k)

        per_query.append({
            "description": q["description"],
            "query": q["query"],
            "relevant_human": list(q["relevant"]),
            "retrieved_human": [reverse_map.get(i, i) for i in retrieved[:k]],
            "recall@1": round(r1, 3),
            "recall@3": round(r3, 3),
            f"recall@{k}": round(r5, 3),
            f"precision@{k}": round(p_k, 3),
            "mrr": round(rr, 3),
            f"ndcg@{k}": round(ndcg, 3),
            "latency_ms": round(latency_ms, 1),
        })

        if verbose:
            hit = r5 > 0
            status = "HIT " if hit else "MISS"
            print(
                f"  [{status}] {q['description']:<20} "
                f"R@1={r1:.2f}  R@3={r3:.2f}  R@{k}={r5:.2f}  "
                f"P@{k}={p_k:.2f}  MRR={rr:.2f}  nDCG@{k}={ndcg:.2f}  "
                f"{latency_ms:.0f}ms"
            )
            retrieved_human = [reverse_map.get(i, i) for i in retrieved[:k]]
            print(f"         retrieved : {retrieved_human}")
            print(f"         relevant  : {sorted(q['relevant'])}")
            if output.startswith("error"):
                print(f"         ERROR     : {output}")
            print()

    # Aggregate
    n = len(per_query)
    agg = {
        "recall@1":      round(sum(r["recall@1"]           for r in per_query) / n, 3),
        "recall@3":      round(sum(r["recall@3"]           for r in per_query) / n, 3),
        f"recall@{k}":   round(sum(r[f"recall@{k}"]       for r in per_query) / n, 3),
        f"precision@{k}": round(sum(r[f"precision@{k}"]   for r in per_query) / n, 3),
        "mrr":           round(sum(r["mrr"]                for r in per_query) / n, 3),
        f"ndcg@{k}":     round(sum(r[f"ndcg@{k}"]         for r in per_query) / n, 3),
        "latency_mean_ms": round(sum(latencies) / n, 1),
        "latency_p95_ms":  round(sorted(latencies)[int(0.95 * n)], 1),
    }

    print("=" * 60)
    print(f"Results  (k={k}, {n} queries)")
    print("=" * 60)
    print(f"  Recall@1       : {agg['recall@1']:.3f}  <- primary metric")
    print(f"  Recall@3       : {agg['recall@3']:.3f}")
    print(f"  Recall@{k:<2}      : {agg[f'recall@{k}']:.3f}")
    print(f"  Precision@{k}   : {agg[f'precision@{k}']:.3f}")
    print(f"  MRR            : {agg['mrr']:.3f}")
    print(f"  nDCG@{k}        : {agg[f'ndcg@{k}']:.3f}")
    print(f"  Latency (mean) : {agg['latency_mean_ms']:.0f}ms")
    print(f"  Latency (p95)  : {agg['latency_p95_ms']:.0f}ms")
    print("=" * 60)

    # Save JSON results
    RESULTS_DIR.mkdir(exist_ok=True)
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    result_file = RESULTS_DIR / f"{ts}_k{k}.json"
    result_data = {
        "timestamp": ts,
        "git": _git_info(),
        "config": {"k": k, "token_budget": token_budget, "num_queries": n},
        "aggregate": agg,
        "per_query": per_query,
    }
    result_file.write_text(json.dumps(result_data, indent=2), encoding="utf-8")
    print(f"\nResults saved -> {result_file.relative_to(pathlib.Path(__file__).parent.parent)}")

    # Cleanup
    col.delete_many({"project": project})
    print(f"Cleaned up {len(SEED_CHUNKS)} benchmark chunks.")

    # Regression comparison against the most recent previous result
    if compare:
        baseline_path, baseline = _load_latest_result(RESULTS_DIR, exclude=result_file.name)
        if baseline is None:
            print("\nNo previous result found to compare against.")
        else:
            regressed = _compare_results(baseline_path, baseline, result_data, threshold)
            if regressed:
                sys.exit(1)

    return agg


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="memory_map retrieval benchmark")
    parser.add_argument("--k",           type=int,   default=5,    help="Recall/Precision/nDCG cutoff (default: 5)")
    parser.add_argument("--budget",      type=int,   default=4000, help="Token budget for suggest_history (default: 4000)")
    parser.add_argument("--index-wait",  type=int,   default=0,    help="Seconds to wait after seeding for Atlas vector index sync (default: 0; use 20 when EMBED_PROVIDER=openai)")
    parser.add_argument("--verbose",     action="store_true",       help="Print per-query breakdown")
    parser.add_argument("--compare",     action="store_true",       help="Compare against the most recent previous result; exit 1 on regression")
    parser.add_argument("--threshold",   type=float, default=0.05,  help="Absolute drop threshold for primary metrics before flagging a regression (default: 0.05)")
    args = parser.parse_args()

    run_benchmark(
        k=args.k, token_budget=args.budget, verbose=args.verbose,
        index_wait=args.index_wait, compare=args.compare, threshold=args.threshold,
    )

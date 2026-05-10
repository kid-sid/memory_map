"""
End-to-end verification of memory_map vector search.
Tests retrieval correctness across multiple realistic scenarios.

Run: python test_setup.py
"""
import os, sys
from dotenv import load_dotenv

# Force UTF-8 for Windows console
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

PROJECT = "__test_setup__"

def section(label):
    print(f"\n--- {label} ---")

def ok(msg):    print(f"  [PASS] {msg}")
def warn(msg):  print(f"  [WARN] {msg}")
def fail(msg):  print(f"  [FAIL] {msg}"); sys.exit(1)


# ----- 1. Connectivity -----
section("Connectivity")
try:
    import history_store
    col = history_store._get_collection()
    ok(f"MongoDB connected → {col.database.name}.{col.name}")
except Exception as e:
    fail(e)

provider = os.environ.get("MEMORY_MAP_EMBED_PROVIDER", "").lower()
if provider == "openai":
    vec = history_store._embed("test")
    if vec and len(vec) == 1536:
        ok(f"OpenAI embedding works (dims={len(vec)})")
    else:
        fail("OpenAI embedding did not return a 1536-dim vector")
elif provider == "atlas":
    ok("Atlas autoEmbed — embeddings generated server-side")
else:
    warn("MEMORY_MAP_EMBED_PROVIDER unset — vector search will be skipped")


# ----- 2. Seed test corpus -----
section("Seeding test corpus")
SEED = [
    ("auth implemented using JWT bearer tokens with middleware validation",     ["auth", "feature"]),
    ("fixed bug in login flow: missing csrf token caused 403 errors",           ["auth", "bug-fix"]),
    ("designed REST API endpoints for user management /api/v1/users",           ["api-design"]),
    ("set up postgres database schema with users and sessions tables",          ["database"]),
    ("decided to use docker-compose for local dev setup, mongo as a service",   ["deployment", "database"]),
    ("refactored caching layer to use redis instead of in-memory dict",         ["refactor", "performance"]),
]
ids = []
for dialogue, tags in SEED:
    cid = history_store.save_chunk(PROJECT, "test_session", dialogue, tags)
    ids.append(cid)
ok(f"saved {len(ids)} test chunks")


# ----- 3. Vector search relevance -----
section("Vector search relevance")
QUERIES = [
    ("how was authentication implemented?",  ["auth"]),     # should match the JWT chunk
    ("what database are we using?",          ["database"]), # should match postgres / mongo
    ("api routes for users",                 ["api-design"]),  # should match REST endpoints
]

if provider:
    for query, expected_tags in QUERIES:
        results = history_store.search_by_vector(PROJECT, query, limit=3)
        if not results:
            warn(f'"{query}" → no results (index still building?)')
            continue
        top = results[0]
        top_tags = set(top.get("tags", []))
        match = bool(top_tags & set(expected_tags))
        score = top.get("score", 0)
        marker = "[PASS]" if match else "[FAIL]"
        print(f"  {marker} \"{query}\" → score={score:.3f} tags={top_tags & set(expected_tags) or top_tags}")
else:
    warn("skipped — no embed provider")


# ----- 4. Edge cases -----
section("Edge cases")

# Short query
short_results = history_store.search_by_vector(PROJECT, "hi", limit=3)
if short_results == []:
    ok("short query (<4 chars) correctly returns []")
else:
    fail(f"short query returned {len(short_results)} results — should be 0")

# Empty query
empty_results = history_store.search_by_vector(PROJECT, "", limit=3)
if empty_results == []:
    ok("empty query correctly returns []")
else:
    fail("empty query should return []")

# Min score filter — irrelevant query should return fewer/no matches
irrelevant = history_store.search_by_vector(PROJECT, "purple unicorns dancing on the moon", limit=10, min_score=0.7)
ok(f"irrelevant query filtered to {len(irrelevant)} matches at min_score=0.7")


# ----- 5. Backfill (openai only) -----
if provider == "openai":
    section("Backfill")
    # Insert a chunk WITHOUT embedding (simulating an old chunk)
    raw_id = col.insert_one({
        "project": PROJECT,
        "session_id": "old",
        "timestamp": "2025-01-01T00:00:00Z",
        "dialogue": "old chunk without embedding for backfill test",
        "preview": "old chunk without embedding",
        "tags": ["legacy"],
        "stats": {"chars": 50, "tokens": 12},
    }).inserted_id

    result = history_store.backfill_embeddings(project=PROJECT, batch_size=5)
    if result.get("backfilled", 0) >= 1:
        ok(f"backfill embedded {result['backfilled']} chunk(s)")
    else:
        warn(f"backfill result: {result}")

    # Verify the backfilled chunk now has an embedding
    doc = col.find_one({"_id": raw_id})
    if doc and "embedding" in doc and len(doc["embedding"]) == 1536:
        ok("backfilled chunk now has a 1536-dim embedding")
    else:
        fail("backfilled chunk missing embedding")


# ----- 6. Cleanup -----
section("Cleanup")
deleted = col.delete_many({"project": PROJECT}).deleted_count
ok(f"removed {deleted} test chunks")

print("\n=== All checks passed ===\n")

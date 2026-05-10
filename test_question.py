"""
Simulates a real session-start flow:
1. Seed realistic conversation history
2. Ask a question via suggest_history (the function Claude calls)
3. Print exactly what Claude would receive
"""
import os, sys
from dotenv import load_dotenv

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import history_store
import server

PROJECT = "__test_question__"
col = history_store._get_collection()

# Clean any prior test data
col.delete_many({"project": PROJECT})

# Seed realistic history (different topics, varied recency would matter in prod)
SEED = [
    "user: how should we handle login?\nassistant: implemented JWT bearer tokens with a fastapi middleware that validates on every protected route. tokens expire in 24h, refresh via /auth/refresh endpoint.",
    "user: what about logout?\nassistant: added /auth/logout that adds the JWT to a redis blocklist with TTL matching the token expiry. middleware checks blocklist before validating signature.",
    "user: design the user table\nassistant: created users table with id (uuid), email (unique), password_hash (bcrypt), created_at, updated_at. added partial index on email for case-insensitive lookups.",
    "user: pick a deployment strategy\nassistant: going with docker-compose for local dev and kubernetes via helm for prod. rolling deployment with 2 replicas minimum, health checks on /health.",
    "user: caching layer?\nassistant: redis cluster with 3 nodes, key TTL of 5min for user profile data, cache-aside pattern. invalidate on write via pub/sub.",
    "user: fix the broken signup flow\nassistant: bug was missing email verification step. added /auth/verify endpoint that takes a token sent via SES, marks email_verified=true on the user row.",
]

print("--- Seeding history ---")
for dialogue in SEED:
    cid = history_store.save_chunk(PROJECT, "demo_session", dialogue, history_store.extract_tags(dialogue))
    print(f"  saved chunk {cid[:8]}... tags={history_store.extract_tags(dialogue)}")

# Realistic question that doesn't use the exact words from any saved chunk
QUESTION = "how was authentication implemented in this project?"

print(f"\n--- Asking: '{QUESTION}' ---\n")
result = server.suggest_history(PROJECT, QUESTION, token_budget=2000)
print(result)

# Cleanup
print("\n--- Cleanup ---")
deleted = col.delete_many({"project": PROJECT}).deleted_count
print(f"  removed {deleted} chunks")

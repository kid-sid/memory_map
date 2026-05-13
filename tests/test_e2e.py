"""
Solution/E2E tests: full user journeys across the complete stack.

These tests verify behaviors that unit tests cannot confirm in isolation —
specifically, that history_hook.py (a subprocess) writes data that the MCP
server tools can then read back correctly.

Coverage:
  - Journey 1: hook fires → saves chunk → MCP tools retrieve it
  - Journey 2: multiple hook fires produce a newest-first index
  - Journey 3: tag index enables token-budget-aware selective fetch
  - Journey 4: smoke pass — every MCP tool responds without error
"""

import json
import os
import pathlib
import subprocess
import sys
import uuid

import pytest

import history_store
from server import (
    get_history_chunks,
    get_project_summary,
    load_history,
    load_memory,
    save_history,
    save_memory,
    suggest_history,
)

PROJECT_ROOT = pathlib.Path(__file__).parent.parent
HOOK_SCRIPT = str(PROJECT_ROOT / "history_hook.py")

# ---------------------------------------------------------------------------
# Realistic dialogue samples — sized to hit specific token ranges so tests
# that exercise token-budget logic use meaningful data instead of filler.
# compute_stats() uses chars//4 for tokens, so sizes are in characters.
# ---------------------------------------------------------------------------

# ~56 chars ≈ 14 tokens  (budget tests need this << 150)
_DIALOGUE_SMALL = (
    "user: fix the off-by-one error in the pagination helper\n"
    "assistant: fixed — page index now starts at 0, updated test_pagination.py"
)

# ~440 chars ≈ 110 tokens  (budget tests need this ≤ 150 tokens)
_DIALOGUE_MEDIUM = (
    "user: /api/login returns 500 when the email field is empty\n"
    "assistant: added null check in validate_credentials() — returns 400 "
    "with error message if email or password is blank\n"
    "user: also handle locked accounts\n"
    "assistant: added account_locked check after credential validation, "
    "returns 403 with reason in json body\n"
    "user: rate-limit failed logins\n"
    "assistant: Redis counter keyed by IP, 429 after 5 fails per minute, "
    "Retry-After header included"
)

# ~1243 chars ≈ 310 tokens  (budget tests need this > 150 tokens)
_DIALOGUE_LARGE = (
    "user: the UserService is 800 lines — handles auth, profiles, password "
    "resets, and sessions. help split it\n"
    "assistant: splitting into four focused services:\n"
    "  AuthService — login, logout, token refresh\n"
    "  ProfileService — update profile, avatar, deactivate\n"
    "  PasswordService — forgot/reset/change password\n"
    "  VerificationService — email and phone token flows\n"
    "starting with AuthService, injecting deps via constructor: "
    "AuthService(user_repo, session_store, token_config)\n"
    "user: what is token_config\n"
    "assistant: a dataclass holding JWT secret and TTLs — currently scattered "
    "as hardcoded strings in UserService, centralising makes key rotation a "
    "single config change\n"
    "user: where does session management go\n"
    "assistant: SessionStore in storage/session_store.py wraps create_session, "
    "invalidate_session, get_session_data. AuthService depends on it but "
    "SessionStore has zero auth logic so the dependency is clean\n"
    "user: how do we migrate the existing tests\n"
    "assistant: keep UserService as a thin facade that delegates to the new "
    "services — all current imports keep working. migrate test files one by "
    "one then delete the facade\n"
    "user: any circular imports\n"
    "assistant: none — graph is controllers → services → storage → infra, "
    "nothing in storage imports from services"
)

# ~914 chars ≈ 228 tokens per chunk  (5 of these total > budget of 600 tokens)
_FEATURE_CHUNK = (
    "user: add rate limiting to the public API before the beta launch\n"
    "assistant: using a token bucket backed by Redis. each client gets N "
    "tokens that refill at a fixed rate. on each request consume one token; "
    "if the bucket is empty return 429 with Retry-After. middleware in "
    "middleware/rate_limit.py, configurable per route via a FastAPI dependency\n"
    "user: per IP or per API key\n"
    "assistant: both with different quotas. unauthenticated: 60 req/min by IP. "
    "authenticated: 300 req/min by API key. middleware checks Authorization "
    "header first and uses it as the bucket key when present. prevents abuse "
    "from shared IPs while protecting against key misuse\n"
    "user: how do we test without a real Redis instance\n"
    "assistant: fakeredis — implements the Redis protocol in memory. "
    "monkeypatch get_redis() in pytest to return FakeRedis. the Lua script "
    "runs identically. test cases: first request allowed, Nth+1 blocked, "
    "refill after window, concurrent requests at the limit"
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def make_transcript(directory: pathlib.Path, exchanges: list) -> pathlib.Path:
    """Write a minimal Claude transcript JSONL the hook can parse."""
    transcript = directory / "transcript.jsonl"
    with open(transcript, "w") as f:
        for ex in exchanges:
            entry = {
                "type": ex["role"],
                "message": {"role": ex["role"], "content": ex["content"]},
            }
            f.write(json.dumps(entry) + "\n")
    return transcript


def run_hook(cwd: pathlib.Path, transcript: pathlib.Path,
             session_id: str = "", force: bool = True,
             env_overrides: dict = None) -> dict:
    """Invoke history_hook.py as a subprocess; return its parsed stdout.

    A unique session_id is generated per call by default so stale watermark
    files from previous test runs never interfere.
    env_overrides: optional extra env vars (e.g. {"MCP_MAX_CHUNK_CHARS": "200"}).
    """
    if not session_id:
        session_id = uuid.uuid4().hex  # unique → no temp-file collision
    payload = json.dumps({
        "session_id": session_id,
        "transcript_path": str(transcript),
        "cwd": str(cwd),
    })
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    if env_overrides:
        env.update(env_overrides)

    result = subprocess.run(
        [sys.executable, HOOK_SCRIPT, *(["--force"] if force else [])],
        input=payload,
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    assert result.returncode == 0, (
        f"hook exited {result.returncode}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return json.loads(result.stdout.strip() or "{}")


# ---------------------------------------------------------------------------
# Journey 1: hook fires → MCP tools retrieve the chunk
# ---------------------------------------------------------------------------

def test_hook_to_mcp_full_journey(tmp_path):
    """
    Full pipeline: hook subprocess saves a chunk, then load_history returns
    the tag index and get_history_chunks returns the full dialogue.
    """
    transcript = make_transcript(tmp_path, [
        {"role": "user",      "content": "fix the database connection bug"},
        {"role": "assistant", "content": "found the issue — fixed the query timeout"},
    ])

    output = run_hook(tmp_path, transcript)

    # Hook confirms save: one pair, tags, token count
    assert "systemMessage" in output
    msg = output["systemMessage"]
    assert "[history]" in msg
    assert "pair(s) saved" in msg
    assert "tokens:" in msg

    # load_history returns the tag index, not raw dialogue
    index_result = load_history(str(tmp_path))
    assert "=== History Index ===" in index_result
    assert "get_history_chunks" in index_result
    assert "tokens:" in index_result

    # Exactly one chunk was saved
    index = history_store.load_index(str(tmp_path), last_n=10)
    assert len(index) == 1

    # get_history_chunks fetches the full text for that chunk ID
    chunk_id = index[0]["id"]
    fetch_result = get_history_chunks(str(tmp_path), chunk_id)
    assert "total_tokens:" in fetch_result
    assert "database connection bug" in fetch_result or "query timeout" in fetch_result


# ---------------------------------------------------------------------------
# Journey 2: multiple hook fires → index is newest-first
# ---------------------------------------------------------------------------

def test_hook_multiple_fires_newest_first(tmp_path):
    """
    Two sequential hook fires (different session IDs so independent watermarks)
    produce two chunks; the index returns them newest-first.
    """
    transcript = make_transcript(tmp_path, [
        {"role": "user",      "content": "implement the login feature"},
        {"role": "assistant", "content": "done"},
    ])

    run_hook(tmp_path, transcript, session_id=uuid.uuid4().hex)
    run_hook(tmp_path, transcript, session_id=uuid.uuid4().hex)

    index = history_store.load_index(str(tmp_path), last_n=10)
    assert len(index) == 2
    # Newest chunk must come first (compare timestamps — IDs may be ObjectIds in MongoDB)
    assert index[0]["timestamp"] >= index[1]["timestamp"]


# ---------------------------------------------------------------------------
# Journey 3: tag index enables token-budget-aware selective fetch
# ---------------------------------------------------------------------------

def test_token_budget_selective_fetch(tmp_path):
    """
    Three chunks of wildly different sizes are saved. The caller reads the index,
    selects only chunks affordable within a token budget, and fetches them.
    total_tokens for the fetched set stays within budget.
    """
    id_small  = history_store.save_chunk(str(tmp_path), "s", _DIALOGUE_SMALL,  ["testing"])   # ~14 tokens
    id_medium = history_store.save_chunk(str(tmp_path), "s", _DIALOGUE_MEDIUM, ["bug-fix"])   # ~110 tokens
    id_large  = history_store.save_chunk(str(tmp_path), "s", _DIALOGUE_LARGE,  ["refactor"])  # ~310 tokens

    index = history_store.load_index(str(tmp_path), last_n=10)

    budget = 150  # tokens
    affordable_ids = [e["id"] for e in index if e["stats"]["tokens"] <= budget]
    # id_large (1000 tokens) must be excluded
    assert id_large not in affordable_ids

    chunks, total = history_store.get_chunks(str(tmp_path), affordable_ids)
    assert total <= budget, f"total_tokens {total} exceeded budget {budget}"
    assert all(c["stats"]["tokens"] <= budget for c in chunks)


# ---------------------------------------------------------------------------
# Journey 4: smoke pass — every MCP tool responds without error
# ---------------------------------------------------------------------------

def test_smoke_memory_round_trip(tmp_path):
    project = str(tmp_path)
    assert save_memory(project, "stack", "fastmcp python") == "saved: stack"
    assert "fastmcp python" in load_memory(project)


def test_smoke_history_tools_chain(tmp_path):
    """save_history → load_history → get_history_chunks all succeed."""
    project = str(tmp_path)

    save_result = save_history(project, "user: implement OAuth2\nassistant: done", tags="auth")
    assert "history saved" in save_result
    assert "auth" in save_result

    load_result = load_history(project)
    assert "History Index" in load_result
    assert "auth" in load_result

    index = history_store.load_index(project, last_n=1)
    fetch_result = get_history_chunks(project, index[0]["id"])
    assert "total_tokens:" in fetch_result
    assert "OAuth2" in fetch_result


def test_smoke_get_project_summary(tmp_path):
    project = str(tmp_path)
    save_memory(project, "stack", "python")
    save_history(project, "user: write tests\nassistant: done", tags="testing")
    result = get_project_summary(project)
    assert "Project:" in result
    assert "Keys stored: 1" in result
    assert "testing" in result


@pytest.mark.parametrize("bad_input", ["", "   "])
def test_smoke_get_history_chunks_empty_ids(tmp_path, bad_input):
    result = get_history_chunks(str(tmp_path), bad_input)
    assert result.startswith("error:")


def test_smoke_load_history_empty_project(tmp_path):
    assert load_history(str(tmp_path)) == "no history yet"


# ---------------------------------------------------------------------------
# Journey 5: suggest_history — relevance-ranked, token-budgeted retrieval
# ---------------------------------------------------------------------------

def test_suggest_history_relevant_chunk_included(tmp_path):
    """Chunk whose tags match the user message must be present in output."""
    project = str(tmp_path)
    history_store.save_chunk(project, "s1", "user: fix the login bug\nassistant: patched oauth", ["auth", "bug-fix"])
    history_store.save_chunk(project, "s2", "user: deploy to staging\nassistant: pushed docker image", ["deployment"])
    history_store.save_chunk(project, "s3", "user: write tests for auth\nassistant: added pytest fixtures", ["testing", "auth"])

    result = suggest_history(project, "I need to fix the login flow", token_budget=2000)

    assert "Relevant History" in result
    assert "auth" in result
    assert "login" in result or "oauth" in result


def test_suggest_history_always_includes_most_recent(tmp_path):
    """The most recent chunk is always present regardless of tag relevance."""
    project = str(tmp_path)
    history_store.save_chunk(project, "s1", "user: something unrelated to current task", ["documentation"])
    history_store.save_chunk(project, "s2", "user: deploy pipeline setup", ["deployment"])

    # Ask about database — neither chunk has database tags, but s2 (most recent) must appear
    result = suggest_history(project, "postgres schema migration", token_budget=2000)

    assert "Relevant History" in result
    # s2 is most recent → must be included
    index = history_store.load_index(project, last_n=1)
    assert index[0]["id"] in result


def test_suggest_history_token_budget_respected(tmp_path):
    """total_tokens across returned chunks must not exceed token_budget."""
    project = str(tmp_path)
    # Save 5 chunks of ~228 tokens each (5 × 228 = 1140 > budget of 600)
    for i in range(5):
        history_store.save_chunk(project, "s", _FEATURE_CHUNK, ["feature"])

    result = suggest_history(project, "build the feature", token_budget=600)

    # Parse total_tokens from the header line
    header = [l for l in result.splitlines() if "chunks," in l][0]
    total = int(header.split(",")[1].strip().split()[0])
    assert total <= 600


def test_suggest_history_empty_project(tmp_path):
    assert suggest_history(str(tmp_path), "any message") == "no history yet"


def test_suggest_history_skip_not_stop(tmp_path):
    """A large chunk that exceeds budget should be skipped; smaller later chunks still included."""
    project = str(tmp_path)
    # Chunk 1 (oldest, most relevant tag): small
    history_store.save_chunk(project, "s1", "user: fix auth login\nassistant: done", ["auth"])
    # Chunk 2 (middle): very large — should be skipped
    history_store.save_chunk(project, "s2", "y" * 7600, ["deployment"])  # ~1900 tokens
    # Chunk 3 (newest, anchor): small
    history_store.save_chunk(project, "s3", "user: recent work\nassistant: ok", ["feature"])

    result = suggest_history(project, "fix the login issue", token_budget=300)

    # Chunk 3 (anchor) must be in; chunk 2 (1900 tokens) must be skipped
    assert "recent work" in result
    assert "y" * 50 not in result  # chunk 2 content absent


def test_suggest_history_retrieves_old_relevant_chunk(tmp_path):
    """A chunk with matching tags is found even when outside the last_n recency window."""
    project = str(tmp_path)
    old_id = history_store.save_chunk(
        project, "s0",
        "user: postgres migration failed — column already exists\n"
        "assistant: added IF NOT EXISTS to the ALTER TABLE statement",
        ["database"],
    )
    # Push old chunk outside last_n=10 with 12 generic saves
    for i in range(12):
        history_store.save_chunk(project, "s", f"user: task {i}\nassistant: done {i}", ["feature"])

    # Sanity: old chunk must be outside the recency window
    recent = {e["id"] for e in history_store.load_index(project, last_n=10)}
    assert old_id not in recent, "setup: old chunk must be outside last_n=10"

    result = suggest_history(project, "postgres query migration", token_budget=5000)

    assert "Relevant History" in result
    assert old_id in result or "postgres migration" in result or "IF NOT EXISTS" in result


def test_suggest_history_recency_fallback_no_tags(tmp_path):
    """When user message matches no tags, recent chunks are returned via recency scoring."""
    project = str(tmp_path)
    history_store.save_chunk(project, "s", "user: general question\nassistant: general answer", ["documentation"])

    result = suggest_history(project, "hello", token_budget=2000)

    assert "Relevant History" in result
    assert "general" in result


def test_suggest_history_relevant_chunk_ranked_before_recent(tmp_path):
    """High-relevance chunk must appear before the more-recent but unrelated anchor in output.

    Regression guard for the relevance-first sort introduced in commit c5c9133.
    The relevant chunk gets a positive BM25/RRF score; the unrelated anchor gets
    score 0.0 — so it must sort last.
    """
    project = str(tmp_path)

    # Chunk 1 (older): strong auth + bug-fix signal matching the query
    history_store.save_chunk(
        project, "s1",
        "user: fix JWT token expiry — users are logged out after 5 minutes\n"
        "assistant: extended TTL from 300s to 3600s in auth.py and added "
        "POST /auth/refresh for silent renewal",
        ["auth", "bug-fix"],
    )
    # Chunk 2 (newer = anchor): unrelated deployment content, no auth signal
    history_store.save_chunk(
        project, "s2",
        "user: deploy the frontend build to staging\n"
        "assistant: pushed docker image, pipeline green",
        ["deployment"],
    )

    result = suggest_history(project, "fix the jwt login token expiry auth issue", token_budget=2000)

    assert "Relevant History" in result
    auth_pos = result.find("auth.py")       # unique string from chunk 1
    deploy_pos = result.find("docker image")  # unique string from chunk 2
    assert auth_pos != -1, "auth chunk content missing from output"
    assert deploy_pos != -1, "deployment chunk content missing from output"
    assert auth_pos < deploy_pos, "relevant (auth) chunk must rank before unrelated (deployment) anchor"


# ---------------------------------------------------------------------------
# Journey 6: per-Q&A-pair saving
# ---------------------------------------------------------------------------

def test_hook_saves_one_doc_per_qa_pair(tmp_path):
    """Transcript with 3 complete Q&A pairs → 3 separate documents saved."""
    transcript = make_transcript(tmp_path, [
        {"role": "user",      "content": "fix the null pointer"},
        {"role": "assistant", "content": "fixed in utils.py line 42"},
        {"role": "user",      "content": "add a health check endpoint"},
        {"role": "assistant", "content": "added GET /healthz returning status ok"},
        {"role": "user",      "content": "write tests for the endpoint"},
        {"role": "assistant", "content": "added test_healthz in test_server.py"},
    ])

    run_hook(tmp_path, transcript)

    index = history_store.load_index(str(tmp_path), last_n=10)
    assert len(index) == 3


def test_hook_split_large_qa_pair(tmp_path):
    """A Q&A pair exceeding MCP_MAX_CHUNK_CHARS is split into linked chunks."""
    # Force a tiny chunk size so a short exchange triggers a split
    transcript = make_transcript(tmp_path, [
        {"role": "user",      "content": "describe the refactor plan"},
        {"role": "assistant", "content": "A" * 300},  # total pair > 200 chars
    ])

    run_hook(tmp_path, transcript, env_overrides={"MCP_MAX_CHUNK_CHARS": "200", "MCP_OVERLAP_CHARS": "20"})

    index = history_store.load_index(str(tmp_path), last_n=10)
    assert len(index) >= 2, "large pair should produce multiple chunks"

    # Fetch full chunks and verify group_id + part/total_parts linkage
    ids = ",".join(e["id"] for e in index)
    fetch_result = get_history_chunks(str(tmp_path), ids)
    assert "total_tokens:" in fetch_result

    # All chunks from the same pair share a group_id
    col = history_store._get_collection()
    if col is not None:
        from bson import ObjectId
        docs = list(col.find({"project": str(tmp_path)}))
        group_ids = {d.get("group_id") for d in docs if d.get("group_id")}
        assert len(group_ids) == 1, "all split chunks should share one group_id"
        parts = sorted(d["part"] for d in docs if d.get("part"))
        assert parts == list(range(1, len(parts) + 1))
    else:
        import json as _json
        data = _json.loads((tmp_path / ".mcp_history.json").read_text())
        split_chunks = [c for c in data["chunks"] if c.get("group_id")]
        assert len(split_chunks) >= 2
        group_ids = {c["group_id"] for c in split_chunks}
        assert len(group_ids) == 1

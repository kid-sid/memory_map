"""Tests for the redesigned history system (tag index + full-fetch pattern)."""

import datetime
import pytest
import history_store
from server import save_history, load_history, get_history_chunks


# ---------------------------------------------------------------------------
# history_store unit tests (require MongoDB)
# ---------------------------------------------------------------------------

def test_compute_stats_chars_and_tokens():
    stats = history_store.compute_stats("hello world")
    assert stats["chars"] == len("hello world")
    assert stats["tokens"] == len("hello world") // 4


def test_save_chunk_returns_id(tmp_path, requires_mongodb):
    chunk_id = history_store.save_chunk(str(tmp_path), "sess1", "user: hello\nassistant: hi", [])
    assert chunk_id is not None
    assert chunk_id != ""


def test_load_index_no_dialogue(tmp_path, requires_mongodb):
    """load_index returns preview, not the full dialogue field."""
    dialogue = "user: refactor the db layer\nassistant: sure"
    history_store.save_chunk(str(tmp_path), "s1", dialogue, ["refactor", "database"])
    index = history_store.load_index(str(tmp_path), last_n=5)
    assert len(index) == 1
    entry = index[0]
    assert "id" in entry
    assert "timestamp" in entry
    assert "tags" in entry
    assert "preview" in entry
    assert "stats" in entry
    assert "dialogue" not in entry


def test_load_index_preview_truncated(tmp_path, requires_mongodb):
    long_dialogue = (
        "user: the deploy pipeline breaks on docker build after we restructured "
        "the repo — the COPY command still references the old requirements.txt path\n"
        "assistant: updated COPY ./requirements.txt to COPY ./backend/requirements.txt "
        "in the Dockerfile and confirmed the build passes locally"
    )
    history_store.save_chunk(str(tmp_path), "s1", long_dialogue, [])
    index = history_store.load_index(str(tmp_path))
    assert len(index[0]["preview"]) <= 100


def test_get_chunks_returns_full_dialogue(tmp_path, requires_mongodb):
    dialogue = "user: fix the bug\nassistant: fixed"
    chunk_id = history_store.save_chunk(str(tmp_path), "s1", dialogue, ["bug-fix"])
    chunks, total_tokens = history_store.get_chunks(str(tmp_path), [chunk_id])
    assert len(chunks) == 1
    assert chunks[0]["dialogue"] == dialogue
    assert total_tokens == chunks[0]["stats"]["tokens"]


def test_get_chunks_total_tokens_sum(tmp_path, requires_mongodb):
    id1 = history_store.save_chunk(str(tmp_path), "s", "a" * 40, [])
    id2 = history_store.save_chunk(str(tmp_path), "s", "b" * 80, [])
    chunks, total = history_store.get_chunks(str(tmp_path), [id1, id2])
    expected = sum(c["stats"]["tokens"] for c in chunks)
    assert total == expected


def test_get_chunks_unknown_id_skipped(tmp_path, requires_mongodb):
    history_store.save_chunk(str(tmp_path), "s", "some dialogue", [])
    chunks, total = history_store.get_chunks(str(tmp_path), ["9999"])
    assert chunks == []
    assert total == 0


def test_load_index_last_n(tmp_path, requires_mongodb):
    for i in range(10):
        history_store.save_chunk(str(tmp_path), "s", f"chunk {i}", [])
    index = history_store.load_index(str(tmp_path), last_n=3)
    assert len(index) == 3


def test_get_latest_save_empty(tmp_path):
    assert history_store.get_latest_save(str(tmp_path)) == ""


def test_get_latest_save_after_chunk(tmp_path, requires_mongodb):
    history_store.save_chunk(str(tmp_path), "s", "hello", [])
    ts = history_store.get_latest_save(str(tmp_path))
    assert ts != ""
    assert "T" in ts  # ISO-8601


def test_extract_tags_keyword_match():
    tags = history_store.extract_tags("user: fix the login bug\nassistant: done")
    assert "bug-fix" in tags

def test_extract_tags_scores_by_hits():
    """Tag with more keyword hits ranks first."""
    dialogue = "user: test test test pytest fixture\nassistant: fixed bug"
    tags = history_store.extract_tags(dialogue)
    assert tags[0] == "testing"

def test_extract_tags_max_five():
    long = "auth login oauth bug fix error test pytest docker deploy pipeline etl"
    tags = history_store.extract_tags(long)
    assert len(tags) <= 5

def test_extract_tags_no_match_returns_empty():
    tags = history_store.extract_tags("user: hello\nassistant: hi there")
    assert tags == []


# ---------------------------------------------------------------------------
# MCP tool interface tests
# ---------------------------------------------------------------------------

def test_mcp_save_and_load_index(tmp_path, requires_mongodb):
    project = str(tmp_path)
    save_history(project, "user: fixed db query\nassistant: committed")
    result = load_history(project)
    assert "=== History Index ===" in result
    assert "get_history_chunks" in result


def test_mcp_load_empty(tmp_path):
    assert load_history(str(tmp_path)) == "no history yet"


def test_mcp_load_last_n(tmp_path, requires_mongodb):
    project = str(tmp_path)
    for i in range(6):
        save_history(project, f"user: task {i}\nassistant: done")
    result = load_history(project, last_n=3)
    lines = [l for l in result.splitlines() if l.startswith("[")]
    assert len(lines) == 3


def test_mcp_save_with_explicit_tags(tmp_path, requires_mongodb):
    project = str(tmp_path)
    result = save_history(project, "user: deploy to prod\nassistant: done", tags="deployment,configuration")
    assert "deployment" in result
    assert "configuration" in result


def test_mcp_get_history_chunks(tmp_path, requires_mongodb):
    project = str(tmp_path)
    save_history(project, "user: refactor auth module\nassistant: refactored")
    index = history_store.load_index(project, last_n=1)
    chunk_id = index[0]["id"]

    result = get_history_chunks(project, chunk_id)
    assert "total_tokens:" in result
    assert "refactor auth module" in result


def test_mcp_get_history_chunks_no_ids(tmp_path):
    result = get_history_chunks(str(tmp_path), "")
    assert result.startswith("error:")


def test_mcp_get_history_chunks_bad_ids(tmp_path):
    result = get_history_chunks(str(tmp_path), "nonexistent")
    assert "no chunks found" in result


def test_mcp_stats_in_index(tmp_path, requires_mongodb):
    project = str(tmp_path)
    dialogue = (
        "user: add a GET /healthz endpoint that returns the service version\n"
        "assistant: added to server.py, returns json with status, version, and uptime in seconds"
    )
    save_history(project, dialogue)
    result = load_history(project)
    assert "tokens:" in result


def test_mcp_session_id_stored(tmp_path, requires_mongodb):
    project = str(tmp_path)
    save_history(project, "user: hello\nassistant: hi", session_id="abc123xyz")
    index = history_store.load_index(project, last_n=1)
    chunks, _ = history_store.get_chunks(project, [index[0]["id"]])
    assert chunks[0].get("session_id") == "abc123xy"


def test_save_chunk_split_fields_round_trip(tmp_path, requires_mongodb):
    """group_id / part / total_parts survive a save → get_chunks round-trip."""
    project = str(tmp_path)
    gid = "abc12345"
    history_store.save_chunk(project, "s1", "user: part 1\nassistant: text a", ["feature"],
                             group_id=gid, part=1, total_parts=2)
    history_store.save_chunk(project, "s1", "user: part 2\nassistant: text b", ["feature"],
                             group_id=gid, part=2, total_parts=2)

    index = history_store.load_index(project, last_n=10)
    assert len(index) == 2

    ids = [e["id"] for e in index]
    chunks, _ = history_store.get_chunks(project, ids)
    split_chunks = [c for c in chunks if c.get("group_id")]
    assert len(split_chunks) == 2
    assert all(c["group_id"] == gid for c in split_chunks)
    parts = sorted(c["part"] for c in split_chunks)
    assert parts == [1, 2]
    assert all(c["total_parts"] == 2 for c in split_chunks)


def test_save_chunk_no_split_fields_when_unsplit(tmp_path, requires_mongodb):
    """A regular (unsplit) chunk has no group_id / part / total_parts fields."""
    project = str(tmp_path)
    history_store.save_chunk(project, "s1", "user: hello\nassistant: hi", [])
    index = history_store.load_index(project)
    chunks, _ = history_store.get_chunks(project, [index[0]["id"]])
    c = chunks[0]
    assert "group_id" not in c
    assert "part" not in c
    assert "total_parts" not in c


# ---------------------------------------------------------------------------
# score_chunks unit tests
# ---------------------------------------------------------------------------

def _make_entry(chunk_id, tags, preview, age_days=0):
    """Build a minimal index entry with a timestamp offset by age_days."""
    ts = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(days=age_days)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "id": str(chunk_id),
        "timestamp": ts,
        "tags": tags,
        "preview": preview,
        "stats": {"chars": 100, "tokens": 25},
    }


def test_score_tag_keyword_beats_name_match():
    """'fix the login flow' should hit auth via keyword 'login', not tag name."""
    auth_chunk = _make_entry("1", ["auth"], "user: fix login", age_days=5)
    deploy_chunk = _make_entry("2", ["deployment"], "user: push docker", age_days=5)
    scored = history_store.score_chunks([auth_chunk, deploy_chunk], "fix the login flow")
    top_id = scored[0][1]["id"]
    assert top_id == "1", "auth chunk should score higher — 'login' hits auth keywords"


def test_score_recency_breaks_equal_tag_ties():
    """Two chunks with the same tags — the newer one must rank first."""
    old = _make_entry("1", ["bug-fix"], "fix error", age_days=10)
    new = _make_entry("2", ["bug-fix"], "fix crash", age_days=0)
    scored = history_store.score_chunks([old, new], "fix the bug")
    assert scored[0][1]["id"] == "2", "newer chunk should win the tie"


def test_score_preview_overlap_boosts_rank():
    """A chunk whose preview shares words with the message ranks above a keyword-only match."""
    keyword_only = _make_entry("1", ["deployment"], "update schema", age_days=1)
    preview_match = _make_entry("2", ["feature"], "postgres query timeout fix", age_days=1)
    scored = history_store.score_chunks(
        [keyword_only, preview_match], "postgres query optimization"
    )
    assert scored[0][1]["id"] == "2"


def test_score_empty_message_sorts_by_recency():
    """With no user message, recency alone drives order (newest first)."""
    old = _make_entry("1", ["refactor"], "clean up code", age_days=7)
    new = _make_entry("2", ["testing"], "add pytest", age_days=0)
    scored = history_store.score_chunks([old, new], "")
    assert scored[0][1]["id"] == "2"


def test_score_returns_all_entries():
    entries = [_make_entry(str(i), ["feature"], f"task {i}", age_days=i) for i in range(5)]
    scored = history_store.score_chunks(entries, "new feature")
    assert len(scored) == 5


def test_score_rich_tags_not_penalised():
    """A chunk with more tags scores at least as well as a sparse one when keyword hits are equal."""
    sparse = _make_entry("1", ["auth"], "fix login", age_days=1)
    rich = _make_entry("2", ["auth", "deployment", "testing", "feature", "refactor"], "fix login", age_days=1)
    scored = history_store.score_chunks([sparse, rich], "fix the login flow")
    scores = {e["id"]: s for s, e in scored}
    assert scores["2"] >= scores["1"], "extra non-matching tags must not lower the score"


def test_score_no_tags_chunk_ranked_by_recency_and_preview():
    """Untagged chunks get tag_score=0 but still get recency + preview signals."""
    tagged_old = _make_entry("1", ["auth"], "login fix", age_days=20)
    untagged_new = _make_entry("2", [], "performance benchmark results", age_days=0)
    scored = history_store.score_chunks([tagged_old, untagged_new], "fix the performance issue")
    assert scored[0][1]["id"] == "2"


# ---------------------------------------------------------------------------
# rrf_merge unit tests
# ---------------------------------------------------------------------------

def _e(eid: str) -> dict:
    return {"id": eid, "preview": "", "bm25_text": ""}


def test_rrf_merge_dual_list_beats_single():
    """A chunk appearing in both ranked lists must rank above one in only one list."""
    dual = _e("dual")
    single = _e("single")
    list1 = [(dual, 0.9, "vector"), (single, 0.8, "vector")]
    list2 = [(dual, 0.7, "bm25")]
    merged = history_store.rrf_merge([list1, list2])
    ids = [e["id"] for _, e, _ in merged]
    assert ids[0] == "dual", "chunk in both lists should rank first"


def test_rrf_merge_single_list_preserves_order():
    """RRF over a single list preserves the original ranking order."""
    e1, e2, e3 = _e("1"), _e("2"), _e("3")
    ranked = [(e1, 0.9, "bm25"), (e2, 0.5, "bm25"), (e3, 0.1, "bm25")]
    merged = history_store.rrf_merge([ranked])
    ids = [e["id"] for _, e, _ in merged]
    assert ids == ["1", "2", "3"]


def test_rrf_merge_source_label_combined():
    """A chunk in both lists gets a combined source label containing both names."""
    e = _e("x")
    merged = history_store.rrf_merge([[(e, 0.9, "vector")], [(e, 0.5, "bm25")]])
    _, _, source = merged[0]
    assert "bm25" in source and "vector" in source


def test_rrf_merge_empty_lists_returns_empty():
    assert history_store.rrf_merge([]) == []


def test_rrf_merge_k_affects_scores_not_order():
    """Different k values change the magnitude of RRF scores but not their relative order."""
    e1, e2 = _e("1"), _e("2")
    ranked = [(e1, 0.9, "bm25"), (e2, 0.5, "bm25")]
    for k in (1, 60, 100):
        merged = history_store.rrf_merge([ranked], k=k)
        ids = [e["id"] for _, e, _ in merged]
        assert ids == ["1", "2"], f"order changed with k={k}"


# ---------------------------------------------------------------------------
# mmr_rerank unit tests
# ---------------------------------------------------------------------------

def _me(eid: str, tags: list, text: str) -> dict:
    return {"id": eid, "tags": tags, "preview": text, "bm25_text": text}


def test_mmr_zero_diversity_preserves_order():
    """diversity=0.0 disables MMR — original order must be unchanged."""
    e1 = _me("1", ["auth"], "fix login token expiry jwt")
    e2 = _me("2", ["auth"], "fix login token expiry jwt")
    merged = [(0.9, e1, "bm25"), (0.8, e2, "bm25")]
    result = history_store.mmr_rerank(merged, diversity=0.0)
    assert [e["id"] for _, e, _ in result] == ["1", "2"]


def test_mmr_selects_diverse_over_duplicate():
    """A near-duplicate second chunk should be ranked below a diverse third chunk."""
    e1 = _me("1", ["auth", "bug-fix"], "fix login token expiry jwt session")
    e2 = _me("2", ["auth", "bug-fix"], "fix login token expiry jwt session")   # near-duplicate of e1
    e3 = _me("3", ["database"], "postgres migration alter table schema index")   # diverse

    # e1 > e2 > e3 by rrf_score, but e3 is diverse so MMR should promote it above e2
    merged = [(0.9, e1, "bm25"), (0.7, e2, "bm25"), (0.5, e3, "bm25")]
    result = history_store.mmr_rerank(merged, diversity=0.5)
    ids = [e["id"] for _, e, _ in result]

    assert ids[0] == "1", "top relevance chunk should still be first"
    assert ids[1] == "3", "diverse chunk should beat near-duplicate of first"


def test_mmr_single_candidate_unchanged():
    e1 = _me("1", ["auth"], "login fix")
    merged = [(0.9, e1, "bm25")]
    result = history_store.mmr_rerank(merged, diversity=0.5)
    assert len(result) == 1 and result[0][1]["id"] == "1"


def test_mmr_empty_input_unchanged():
    assert history_store.mmr_rerank([], diversity=0.5) == []


# ---------------------------------------------------------------------------
# summarise_oldest_chunks / get_total_tokens unit tests (require MongoDB)
# ---------------------------------------------------------------------------

def test_get_total_tokens_empty(tmp_path):
    assert history_store.get_total_tokens(str(tmp_path)) == 0


def test_get_total_tokens_after_saves(tmp_path, requires_mongodb):
    project = str(tmp_path)
    history_store.save_chunk(project, "s", "a" * 400, [])   # 100 tokens
    history_store.save_chunk(project, "s", "b" * 400, [])   # 100 tokens
    total = history_store.get_total_tokens(project)
    assert total == 200


def test_summarise_oldest_chunks_reduces_count(tmp_path, requires_mongodb):
    project = str(tmp_path)
    for i in range(5):
        history_store.save_chunk(project, "s", f"user: task {i}\nassistant: done {i}", [])
    result = history_store.summarise_oldest_chunks(project, n=3)
    assert result["summarised"] == 3
    assert "new_chunk_id" in result
    # 5 original - 3 deleted + 1 summary = 3 total
    index = history_store.load_index(project, last_n=20)
    assert len(index) == 3


def test_summarise_oldest_chunks_too_few(tmp_path, requires_mongodb):
    project = str(tmp_path)
    history_store.save_chunk(project, "s", "only one chunk", [])
    result = history_store.summarise_oldest_chunks(project, n=5)
    assert result["summarised"] == 0
    assert "reason" in result


def test_summarise_history_mcp_tool(tmp_path, requires_mongodb):
    from server import summarise_history
    project = str(tmp_path)
    for i in range(4):
        history_store.save_chunk(project, "s", f"user: task {i}\nassistant: done {i}", [])
    result = summarise_history(project, n=3)
    assert result.startswith("summarised: 3")
    assert "tokens_before" in result


def test_summarise_skips_existing_summaries(tmp_path, requires_mongodb):
    """summarise_oldest_chunks should not re-summarise a summary chunk."""
    project = str(tmp_path)
    for i in range(5):
        history_store.save_chunk(project, "s", f"user: task {i}\nassistant: done {i}", [])
    # First summarise
    r1 = history_store.summarise_oldest_chunks(project, n=3)
    assert r1["summarised"] == 3
    # Second summarise should collapse the 2 remaining originals (not the summary)
    r2 = history_store.summarise_oldest_chunks(project, n=5)
    index = history_store.load_index(project, last_n=20)
    # summary from r1 + summary from r2 = 2 chunks
    assert len(index) == 2

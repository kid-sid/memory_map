#!/usr/bin/env python3
"""
Claude Code hook: conversation history persistence.

Fires on UserPromptSubmit (every message), PreCompact, and Stop.
Extracts complete Q&A pairs since the last watermark, saves each pair as its
own MongoDB document.  If a pair exceeds MAX_CHUNK_CHARS it is split into
overlapping chunks linked by group_id + part/total_parts.

Zero LLM calls — tags extracted by local keyword matching.

Usage (configured in .claude/settings.json or settings.local.json):
    echo '{"session_id":"...","transcript_path":"...","cwd":"..."}' | python history_hook.py
    echo '{"session_id":"...","transcript_path":"...","cwd":"..."}' | python history_hook.py --force
"""

import sys
import json
import os
import pathlib
import tempfile
import textwrap
import uuid
from datetime import datetime

from memory_map_mcp import history_store
from memory_map_mcp.redact import redact_secrets

MAX_TURN_CHARS   = int(os.environ.get("MCP_MAX_TURN_CHARS",  "3000"))
MAX_CHUNK_CHARS  = int(os.environ.get("MCP_MAX_CHUNK_CHARS", "4000"))
OVERLAP_CHARS    = int(os.environ.get("MCP_OVERLAP_CHARS",   "100"))
TEMP_FILE_TTL_DAYS = 7


# --- Temp file cleanup ---

def _cleanup_stale_temp_files():
    tmp_dir = pathlib.Path(tempfile.gettempdir())
    cutoff = datetime.now().timestamp() - TEMP_FILE_TTL_DAYS * 86400
    for f in tmp_dir.glob("claude_hist_wm_*.txt"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


# --- Watermark (stored in OS temp dir) ---

def _watermark_path(session_id: str) -> pathlib.Path:
    return pathlib.Path(tempfile.gettempdir()) / f"claude_hist_wm_{session_id[:8]}.txt"


def read_watermark(session_id: str) -> int:
    p = _watermark_path(session_id)
    if p.exists():
        try:
            return int(p.read_text().strip())
        except (ValueError, OSError):
            return 0
    return 0


def write_watermark(session_id: str, line_num: int):
    # Atomic write: write to a temp file then rename so a concurrent reader
    # never sees a partial value.
    p = _watermark_path(session_id)
    fd, tmp = tempfile.mkstemp(dir=p.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(str(line_num))
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- Transcript parsing ---

def extract_qa_pairs(transcript_path: str, watermark: int) -> tuple:
    """Read transcript from watermark, return (pairs, new_watermark).

    pairs: list of {"user": str, "assistant": str} — only complete pairs.
    new_watermark: line index AFTER the last complete pair's assistant line.
    Any trailing unpaired user message is left for the next call.
    """
    raw = []  # list of (role, content, line_end)

    try:
        with open(transcript_path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i < watermark:
                    continue

                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type", "")
                msg = entry.get("message", {})
                role = msg.get("role", "")
                content = msg.get("content", "")

                if entry_type not in ("user", "assistant") or role not in ("user", "assistant"):
                    continue

                parts = []
                if isinstance(content, str):
                    if content.startswith("<local-command") or content.startswith("<command-name>"):
                        continue
                    t = content.strip()
                    if t:
                        parts.append(t)
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            t = block.get("text", "").strip()
                            if t:
                                parts.append(t)
                        elif btype == "tool_use" and role == "assistant":
                            # Capture file-modifying tools so code changes appear in history
                            tool_name = block.get("name", "")
                            inp = block.get("input", {})
                            if tool_name == "Edit":
                                fp = inp.get("file_path", "")
                                new_s = textwrap.dedent(inp.get("new_string", "")).strip()
                                parts.append(f"[Edit: {fp}]\n{new_s[:400]}")
                            elif tool_name == "Write":
                                fp = inp.get("file_path", "")
                                c = textwrap.dedent(inp.get("content", "")).strip()
                                parts.append(f"[Write: {fp}]\n{c[:400]}")
                            elif tool_name in ("Bash", "PowerShell"):
                                cmd = inp.get("command", "")
                                parts.append(f"[{tool_name}: {cmd[:200]}]")

                text = "\n".join(parts)[:MAX_TURN_CHARS]
                if text:
                    raw.append((role, text, i + 1))

    except (OSError, IOError):
        return [], watermark

    # Collapse consecutive same-role entries into turns.
    # A complex multi-tool assistant response produces many transcript entries;
    # we join them all so the saved pair contains the full assistant output,
    # not just the preamble before the first tool call.
    turns = []  # list of [role, combined_text, last_line_end]
    for role, content, line_end in raw:
        if turns and turns[-1][0] == role:
            turns[-1][1] += "\n" + content
            turns[-1][2] = line_end
        else:
            turns.append([role, content, line_end])

    # Pair user + assistant turns
    pairs = []
    new_watermark = watermark
    i = 0
    while i < len(turns) - 1:
        role1, content1, _ = turns[i]
        role2, content2, line_end2 = turns[i + 1]
        if role1 == "user" and role2 == "assistant":
            pairs.append({"user": content1, "assistant": content2})
            new_watermark = line_end2
            i += 2
        else:
            i += 1

    return pairs, new_watermark


# --- Splitting ---

def split_into_chunks(text: str) -> list:
    """Split text into MAX_CHUNK_CHARS chunks with OVERLAP_CHARS overlap."""
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start: start + MAX_CHUNK_CHARS])
        start += MAX_CHUNK_CHARS - OVERLAP_CHARS
    return chunks


# --- Main ---

def main():
    _cleanup_stale_temp_files()

    force = "--force" in sys.argv

    try:
        stdin_data = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, EOFError):
        print("{}")
        return

    session_id = stdin_data.get("session_id", "unknown")
    transcript_path = stdin_data.get("transcript_path", "")
    cwd = stdin_data.get("cwd", "")

    if not cwd:
        print("{}")
        return

    if not transcript_path or not os.path.exists(transcript_path):
        print("{}")
        return

    watermark = read_watermark(session_id)
    pairs, new_watermark = extract_qa_pairs(transcript_path, watermark)

    if not pairs:
        print("{}")
        return

    total_tokens = 0
    all_tags = set()

    for pair in pairs:
        dialogue = redact_secrets(f"user: {pair['user']}\nassistant: {pair['assistant']}")
        tags = history_store.extract_tags(dialogue)
        all_tags.update(tags)
        chunks = split_into_chunks(dialogue)
        n = len(chunks)
        gid = uuid.uuid4().hex[:8] if n > 1 else None

        for idx, chunk in enumerate(chunks, 1):
            history_store.save_chunk(
                cwd,
                session_id[:8],
                chunk,
                tags,
                group_id=gid,
                part=(idx if n > 1 else None),
                total_parts=(n if n > 1 else None),
                embed=False,  # hooks must return quickly; embeddings backfilled separately
            )
            total_tokens += history_store.compute_stats(chunk)["tokens"]

    write_watermark(session_id, new_watermark)

    tag_str = ",".join(sorted(all_tags)) if all_tags else "untagged"
    n_pairs = len(pairs)
    output = {
        "systemMessage": (
            f"[history] {n_pairs} pair(s) saved — tags:[{tag_str}] tokens:{total_tokens}"
        )
    }
    print(json.dumps(output))


if __name__ == "__main__":
    main()

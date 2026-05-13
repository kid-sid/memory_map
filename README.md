# Memory Map — MCP Server for Claude Code

[![CI](https://github.com/kid-sid/memory_map/actions/workflows/ci.yml/badge.svg)](https://github.com/kid-sid/memory_map/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An MCP server that gives Claude persistent memory and conversation history — so it understands your project from the very first message of every session, without you re-explaining anything.

## What it does

- **Project structure** — local directory tree or any GitHub repo, `.gitignore`-aware
- **Git history** — recent commits in a compact format
- **Session memory** — save context once, Claude loads it automatically at session start
- **Conversation history** — saves each Q&A pair as its own document; Claude retrieves the most relevant chunks by tag and token budget at session start
- **Compression** — memory output is token-optimized at 3 levels (raw / compact / dense)
- **Multi-project** — browse memory and history across all your projects from a single tool call
- **Manual save** — `/mem_save` slash command to checkpoint the conversation any time

---

## Prerequisites

- Python 3.10+
- Git installed and on your PATH
- [Claude Code](https://claude.ai/code) CLI installed
- (Optional) MongoDB — local or Atlas — for persistent history storage. Falls back to a local JSON file if not configured.

---

## Setup

### Step 1 — Clone and install

```bash
git clone https://github.com/kid-sid/memory_map.git
cd memory_map
```

**Windows:**
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

**Mac/Linux:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

---

### Step 2 — Configure environment variables

Create a `.env` file in the repo root:

```bash
# MongoDB connection string (optional — falls back to JSON if not set)
# Local:  mongodb://localhost:27017
# Atlas:  mongodb+srv://<user>:<password>@<cluster>.mongodb.net
MEMORY_MAP_MONGO_URI=

# Embedding provider for vector search (optional — BM25-only if not set)
# MEMORY_MAP_EMBED_PROVIDER=openai   # OpenAI text-embedding-3-small (requires OPENAI_API_KEY)
# MEMORY_MAP_EMBED_PROVIDER=local    # sentence-transformers all-MiniLM-L6-v2 (CPU, no API key)
# OPENAI_API_KEY=

# GitHub token for higher API rate limits and private repos (optional)
# GITHUB_TOKEN=
```

If `MEMORY_MAP_MONGO_URI` is left blank, history is stored in `.mcp_history.json` per project.

---

### Step 2b — MongoDB setup (optional)

By default, conversation history is stored in a `.mcp_history.json` file per project (up to 100 chunks). MongoDB is optional but gives you unlimited history, persistence across reinstalls, and faster indexed lookups.

**Install MongoDB Community Server**

Download from [mongodb.com/try/download/community](https://www.mongodb.com/try/download/community).

During installation on Windows, tick **"Install MongoDB as a Windows Service"** — this starts MongoDB automatically on boot. You will never need to open Compass or start it manually.

**Add the URI to your `.env` file:**

```
MEMORY_MAP_MONGO_URI=mongodb://localhost:27017
```

That's all. The server creates the database and collection automatically on first use — no migrations, no `CREATE TABLE`.

**Where MongoDB stores data**

| | Default path |
|---|---|
| Data files | `C:\Program Files\MongoDB\Server\<version>\data\` |
| Log files | `C:\Program Files\MongoDB\Server\<version>\log\` |
| Database | `memory_map` (created automatically) |
| Collection | `memory_map.history` (created automatically) |

**Require MongoDB (no JSON fallback)**

If you want the server to error loudly instead of silently falling back to JSON when MongoDB is unreachable, add:

```
MEMORY_MAP_REQUIRE_MONGO=1
```

Without this flag, the server falls back to JSON with a warning if MongoDB is down. With it, every tool call fails with a clear error until MongoDB is reachable — useful if you rely on MongoDB history and never want silent data splitting.

---

### Step 3 — Register the MCP server with Claude Code

Run this once (replace the path with your actual path). The `-s user` flag makes it available in **all projects**.

**Windows with venv:**
```bash
claude mcp add -s user memory_map C:/Users/yourname/memory_map/venv/Scripts/python.exe C:/Users/yourname/memory_map/server.py
```

**Mac/Linux:**
```bash
claude mcp add -s user memory_map python3 /home/yourname/memory_map/server.py
```

Restart Claude Code after running this.

---

### Step 4 — Verify the server is connected

Open Claude Code and run:

```
/mcp
```

You should see `memory_map` listed with 19 tools. If it's not there, double-check the path in Step 3.

---

### Step 5 — Enable session memory (global or per-project)

You have two options. **Option A is recommended** — it enables memory_map in every project without any per-project setup.

#### Option A — Global CLAUDE.md (works in all projects at once)

Claude Code reads `~/.claude/CLAUDE.md` automatically at the start of every session in every project. Add the session setup block there once and you're done.

Open (or create) the file:

| OS | Path |
|---|---|
| Windows | `C:\Users\yourname\.claude\CLAUDE.md` |
| Mac/Linux | `~/.claude/CLAUDE.md` |

Paste this content:

```markdown
## Session Setup (Required)
At the start of every session, before doing anything else:
1. Call `load_memory` with the current working directory
2. Call `suggest_history` with the current working directory and the user's first message
3. Read both outputs before exploring files or asking questions

Save or update memory entries whenever you learn something worth keeping across sessions.
Use short, lowercase keys: `stack`, `current_work`, `gotchas`, `key_files`. Keep values concise — one or two sentences max.
If something loaded from memory is no longer accurate, update it with `save_memory` using the same key.
Do not call `load_history` + `get_history_chunks` manually at session start — those are for inspection and the /mem_save flow only.
```

That's all. Every project now gets memory and history loaded automatically.

#### Option B — Per-project CLAUDE.md

If you only want memory_map active in specific projects, copy the file into each project root instead:

**Windows:**
```bash
copy C:\Users\yourname\memory_map\CLAUDE.md C:\Users\yourname\your-project\CLAUDE.md
```

**Mac/Linux:**
```bash
cp ~/memory_map/CLAUDE.md ~/your-project/CLAUDE.md
```

Claude Code reads any `CLAUDE.md` at the project root automatically.

---

## Using memory_map in other projects

The MCP server and history hook are registered globally, so they work in every project automatically. The only per-project step is dropping a `CLAUDE.md` file.

### What you do once per project

Copy the `CLAUDE.md` from this repo into your project root (see Step 5 above). That file instructs Claude to load memory and history at the start of every session.

You do **not** need to:
- Re-register the MCP server
- Change the hook configuration
- Create any database or collection
- Set up any project-specific config

### How history is tracked per project

The hook receives the **current working directory** from Claude Code on every trigger. That path is used as the project namespace — so `C:\projects\my-api` and `C:\projects\dashboard` each get completely separate history, even though they share the same MCP server and the same MongoDB collection.

```
my-api session  → chunks stored under project: "C:/projects/my-api"
dashboard session → chunks stored under project: "C:/projects/dashboard"
```

Nothing bleeds between projects.

### What gets saved automatically

| Event | What happens |
|---|---|
| `UserPromptSubmit` | Each completed Q&A pair is saved as its own document |
| `PreCompact` | All unsaved pairs are flushed before context compression |
| `Stop` | All unsaved pairs are flushed when the session ends |

No manual action needed. Each save extracts intent tags (`bug-fix`, `database`, `feature`, etc.) by local keyword matching — no LLM calls required. File edits and shell commands made during the response are also captured alongside the dialogue text.

If `MEMORY_MAP_EMBED_PROVIDER=openai` or `local` is set, embeddings are generated asynchronously in the background via `backfill_history_embeddings` — the hook always returns immediately regardless.

If a Q&A pair exceeds 4000 characters, it is automatically split into overlapping chunks linked by a shared `group_id` so context at chunk boundaries is never lost.

### What Claude does at session start

When you open a project that has `CLAUDE.md`:

1. Calls `load_memory(project_path)` — loads all saved key-value context for this project
2. Calls `suggest_history(project_path, first_user_message)` — scores history chunks by relevance to your first message and returns the best fit within a token budget
3. Has full project context before touching a single file

### Where history is stored

| Backend | Storage location |
|---|---|
| JSON (default) | `<your-project>/.mcp_history.json` — one file, stays in the project directory |
| MongoDB | `memory_map.history` collection — filtered by `project` field (the `cwd`) |

### Per-project memory keys

Use short lowercase keys. Suggested starting set:

| Key | What to store |
|---|---|
| `stack` | Languages, frameworks, databases in use |
| `current_work` | What is actively being built or fixed |
| `gotchas` | Non-obvious constraints, footguns, rules to never break |
| `key_files` | The most important files and what they do |

Claude saves these automatically as it learns your project. You can also save them manually:

```
save_memory("C:/projects/my-api", "stack", "FastAPI + Postgres + Redis")
save_memory("C:/projects/my-api", "gotchas", "never bypass the rate limiter middleware")
```

---

### Step 6 — Enable automatic conversation history

This auto-saves each Q&A pair as it happens — no API key required.

**Configure the hooks globally in `~/.claude/settings.json`:**

Replace the path to match where you cloned this repo.

**Windows (`C:\Users\yourname\.claude\settings.json`):**
```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python C:/Users/yourname/memory_map/history_hook.py",
            "timeout": 10
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python C:/Users/yourname/memory_map/history_hook.py --force",
            "timeout": 15
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python C:/Users/yourname/memory_map/history_hook.py --force",
            "timeout": 15,
            "async": true
          }
        ]
      }
    ]
  }
}
```

**Mac/Linux (`~/.claude/settings.json`):**
```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /home/yourname/memory_map/history_hook.py",
            "timeout": 10
          }
        ]
      }
    ],
    "PreCompact": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /home/yourname/memory_map/history_hook.py --force",
            "timeout": 15
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /home/yourname/memory_map/history_hook.py --force",
            "timeout": 15,
            "async": true
          }
        ]
      }
    ]
  }
}
```

Restart Claude Code. History will now be saved automatically after every Q&A pair, on context compaction, and when the session ends.

---

## How session memory works

**First session** — Claude explores your project and saves what matters:
```
save_memory(path, "stack", "Next.js frontend, FastAPI backend, Postgres")
save_memory(path, "gotchas", "never import db.py directly, always go through models/")
```

**Every session after** — Claude calls `load_memory` first and has full context instantly:
```
[stack] Next.js frontend, FastAPI backend, Postgres
[gotchas] never import db.py directly, always go through models/
```

No file reading. No re-explaining the codebase. Context in one tool call.

---

## How conversation history works

After every Q&A exchange, the hook saves the dialogue to MongoDB (or a local JSON file). Each pair becomes its own document — no mixed-topic blobs. Tags are extracted by **local keyword matching** — no LLM calls, no API keys needed.

**What gets captured per pair:**
- The user message (text)
- The assistant's full response, including text commentary, file edits (`[Edit: path]`), writes (`[Write: path]`), and shell commands (`[Bash: ...]`)

**At session start**, Claude calls `suggest_history`, which uses a hybrid retrieval strategy:

1. **Vector search** (when `MEMORY_MAP_EMBED_PROVIDER=openai` or `local`) — semantic similarity search across all stored chunks using OpenAI or local sentence-transformers embeddings
2. **BM25 scoring** — Okapi BM25 keyword scoring over the last 50 chunks, with query expansion via tag-keyword synonyms
3. **Reciprocal Rank Fusion (RRF)** — merges the vector and BM25 ranked lists; chunks appearing in both lists rank higher than chunks in only one
4. **Anchor** — the most recent chunk is always included for session continuity, ranked by its BM25 score
5. **Token budget** — results are selected relevance-first until the budget is filled

```
=== Relevant History (3 chunks, 650 tokens) ===

[abc123] 2026-05-07T09:40:49Z tags:[bug-fix,database]
user: postgres migration failed — column already exists
assistant: added IF NOT EXISTS to the ALTER TABLE statement
...
```

This means Claude can surface a relevant chunk from 3 months ago if it matches your current task — not just whatever you worked on last.

**Tag vocabulary** (extracted automatically from dialogue content):

`api-design` · `architecture` · `auth` · `bug-fix` · `configuration` · `data-pipeline` · `database` · `debugging` · `deployment` · `documentation` · `feature` · `memory` · `performance` · `refactor` · `testing` · `tooling`

**Storage backends:**

| Backend | When used | Capacity |
|---|---|---|
| MongoDB | `MEMORY_MAP_MONGO_URI` is set | Unlimited, indexed by tag and timestamp |
| JSON file (`.mcp_history.json`) | No URI configured | Up to 100 chunks per project |

---

## Memory compression

Memory output is compressed at read time to save tokens. Three levels:

| Level | Format | Example |
|---|---|---|
| `0` (raw) | `key: value` | `stack: Python server using fastmcp` |
| `1` (compact, default) | `[key] value` | `[stack] Python server using fastmcp` |
| `2` (dense) | abbreviated keys + values | `[stack] py srv using fastmcp` |

```
set_compression(project_path, 1)
```

---

## Multi-project support

Browse and search memory across all your projects at once:

```
list_projects(base_path)                         # all projects with saved memory
get_project_summary(project_path)                # memory + recent history for one project
load_cross_project_memory(base_path, "stack")    # load a specific key from all projects
search_across_projects(base_path, "postgres")    # search memory values across all projects
save_global_memory("name", "Sidhartha")          # user-level facts available in all projects
load_global_memory()                             # load global facts
```

---

## Manual history save — `/mem_save`

Type `/mem_save` in Claude Code at any time to immediately checkpoint the recent conversation. Claude summarizes what was discussed and calls `save_history`.

Useful when conversations get long, before switching topics, or before closing a session mid-task.

---

## Tools reference

### Structure & Git

| Tool | Args | Description |
|---|---|---|
| `get_local_structure` | `path`, `max_depth=5` | Directory tree of a local folder |
| `get_github_structure` | `repo`, `branch="main"` | File tree of a GitHub repo (`owner/repo`) |
| `get_git_history` | `path`, `count=5` | Recent commits as `hash \| subject` |

### Memory

| Tool | Args | Description |
|---|---|---|
| `save_memory` | `project_path`, `key`, `content` | Save or update a context entry |
| `load_memory` | `project_path` | Load all saved context (compressed) |
| `delete_memory` | `project_path`, `key` | Remove a specific entry |
| `set_compression` | `project_path`, `level` | Set compression level (0, 1, or 2) |

### Conversation History

| Tool | Args | Description |
|---|---|---|
| `suggest_history` | `project_path`, `user_message`, `token_budget=2000` | **Primary session-start tool.** Hybrid retrieval: vector search + BM25 merged with RRF. Always includes the most recent chunk; ranks remaining by relevance within the token budget |
| `save_history` | `project_path`, `dialogue`, `session_id`, `tags` | Save a raw conversation chunk with auto-extracted tags |
| `load_history` | `project_path`, `last_n=5` | Load the tag index for recent chunks (id, timestamp, tags, preview, token cost) — use for inspection or `/mem_save` flow |
| `get_history_chunks` | `project_path`, `ids` | Fetch full dialogue for comma-separated chunk IDs + total token sum |
| `backfill_history_embeddings` | `project_path=""`, `batch_size=20` | Generate embeddings for chunks saved before `MEMORY_MAP_EMBED_PROVIDER` was configured. Run repeatedly until `remaining=0` |
| `backfill_bm25_text` | `project_path=""`, `batch_size=100` | Write the `bm25_text` field to chunks saved before this field was introduced. Run once after upgrading; repeat until `remaining=0` |

### Multi-project

| Tool | Args | Description |
|---|---|---|
| `list_projects` | `base_path` | List all projects with saved memory under a directory |
| `get_project_summary` | `project_path` | Memory keys + recent history tag index for a project |
| `load_cross_project_memory` | `base_path`, `query_keys` | Aggregate memory across all projects, optionally filter by key |
| `search_across_projects` | `base_path`, `keyword` | Full-text search across all project memory values |
| `save_global_memory` | `key`, `content` | Save a user-level fact available in all projects |
| `load_global_memory` | — | Load all global facts |

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `MEMORY_MAP_MONGO_URI` | _(unset)_ | MongoDB connection string. Falls back to JSON if not set. |
| `MEMORY_MAP_REQUIRE_MONGO` | _(unset)_ | Set to `1` to error instead of falling back to JSON when MongoDB is unreachable. |
| `MEMORY_MAP_EMBED_PROVIDER` | _(unset)_ | `openai` — vector search via OpenAI `text-embedding-3-small` (1536 dims); `local` — vector search via `sentence-transformers all-MiniLM-L6-v2` (384 dims, CPU, no API key, ~90 MB first-run download). BM25-only if unset. |
| `MEMORY_MAP_MIN_VECTOR_SCORE` | `0.65` | Minimum cosine similarity threshold for vector search results. |
| `OPENAI_API_KEY` | _(unset)_ | Required when `MEMORY_MAP_EMBED_PROVIDER=openai`. |
| `GITHUB_TOKEN` | _(unset)_ | GitHub token for private repos / higher rate limits |
| `MCP_MAX_ENTRY_KB` | `10` | Max size per memory entry in KB |
| `MCP_GIT_TIMEOUT` | `10` | Timeout in seconds for git commands |
| `MCP_MAX_TURN_CHARS` | `3000` | Max characters captured per turn before truncation |
| `MCP_MAX_CHUNK_CHARS` | `4000` | Max characters per saved history chunk; larger Q&A pairs are split into overlapping chunks |
| `MCP_OVERLAP_CHARS` | `100` | Overlap between split chunks so context at boundaries is preserved |
| `MCP_WORKSPACE_ROOT` | _(unset)_ | When set, all `project_path` / `base_path` / `path` arguments must resolve inside this directory. Recommended for shared or multi-project setups. |

---

## Running tests

```bash
python -m pytest tests/ -v
```

119 tests across 5 files:

| File | Coverage |
|---|---|
| `test_compression.py` | 3-level compression, abbreviation, filler removal |
| `test_history.py` | Save/load/fetch, stats, tag extraction, split chunks, MCP tool interface, BM25 relevance scoring, RRF merge |
| `test_e2e.py` | Full hook→storage→MCP retrieval journeys, per-pair saving, split chunks, token budget, suggest_history relevance |
| `test_multi_project.py` | Cross-project tools, global memory |
| `test_validation.py` | Key validation, size limits, corrupted JSON recovery |

---

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=kid-sid/memory_map&type=Date)](https://star-history.com/#kid-sid/memory_map&Date)

---

Built with [FastMCP](https://gofastmcp.com) · [Code of Conduct](CODE_OF_CONDUCT.md)

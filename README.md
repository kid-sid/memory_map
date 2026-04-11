# Memory Map — MCP Server for Claude Code

An MCP server that gives Claude persistent memory and conversation history — so it understands your project from the very first message of every session, without you re-explaining anything.

## What it does

- **Project structure** — local directory tree or any GitHub repo, `.gitignore`-aware
- **Git history** — recent commits in a compact format
- **Session memory** — save context once, Claude loads it automatically at session start
- **Conversation history** — automatically summarizes past conversations so Claude stays in context across sessions
- **Compression** — memory output is token-optimized at 3 levels (raw / compact / dense)
- **Manual save** — `/mem_save` slash command to checkpoint the conversation any time

---

## Prerequisites

- Python 3.10+
- Git installed and on your PATH
- [Claude Code](https://claude.ai/code) CLI installed
- (Optional) OpenAI API key — needed for AI-powered history summarization

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

Note the full path to `server.py` — you'll need it in the next step.
Example: `C:/Users/yourname/memory_map/server.py`

---

### Step 2 — Register the MCP server with Claude Code

Run this once (replace the path with your actual path):

```bash
claude mcp add file-structure python C:/Users/yourname/memory_map/server.py
```

> **Windows with venv:** Use the venv's Python to avoid dependency issues:
> ```bash
> claude mcp add file-structure C:/Users/yourname/memory_map/venv/Scripts/python.exe C:/Users/yourname/memory_map/server.py
> ```

Restart Claude Code after running this.

---

### Step 3 — Verify the server is connected

Open Claude Code and run:

```
/mcp
```

You should see `file-structure` listed with 9 tools. If it's not there, double-check the path in Step 2.

---

### Step 4 — Enable session memory for a project

Copy `CLAUDE.md` into the root of any project you want Claude to remember:

**Windows:**
```bash
copy C:\Users\yourname\memory_map\CLAUDE.md C:\Users\yourname\your-project\CLAUDE.md
```

**Mac/Linux:**
```bash
cp ~/memory_map/CLAUDE.md ~/your-project/CLAUDE.md
```

Claude Code reads `CLAUDE.md` automatically at session start — this tells it to call `load_memory` and `load_history` before doing anything else.

---

### Step 5 — Enable automatic conversation history (optional)

This feature auto-summarizes your conversations every 5 messages and stores them in `.mcp_history.json`. It uses GPT-4o-mini, so you'll need an OpenAI API key.

**Set your API key:**

Windows:
```bash
$env:OPENAI_API_KEY = "sk-..."
```

Mac/Linux:
```bash
export OPENAI_API_KEY="sk-..."
```

To make it permanent, add the export to your shell profile (`.bashrc`, `.zshrc`, or Windows environment variables).

**Configure the hooks in your project's `.claude/settings.local.json`:**

Create or edit `.claude/settings.local.json` in your project root with the following (replace the path to match where you cloned this repo):

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
    ]
  }
}
```

Restart Claude Code. History will now be saved automatically every 5 messages and whenever context compaction happens.

> **Without an OpenAI key:** The hook still works — it saves a raw truncated excerpt instead of an AI summary.

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

Every 5 user messages, the hook reads your conversation transcript, sends it to GPT-4o-mini, and saves a dense summary to `.mcp_history.json`. At session start, Claude calls `load_history` and gets the last 5 summaries — so it knows what you were working on even after days away.

A rolling window of 20 chunks is kept. Older ones are dropped automatically.

---

## Memory compression

Memory output is compressed at read time to save tokens. Three levels are available:

| Level | Format | Example |
|---|---|---|
| `0` (raw) | `key: value` | `stack: Python server using fastmcp` |
| `1` (compact, default) | `[key] value` | `[stack] Python server using fastmcp` |
| `2` (dense) | abbreviated keys + values | `[stack] py srv using fastmcp` |

Set the level per project:
```
set_compression(project_path, 1)
```

---

## Manual history save — `/mem_save`

Type `/mem_save` in Claude Code at any time to immediately checkpoint the recent conversation. Claude will summarize what was discussed and call `save_history` for you.

This is useful when conversations get long, before switching topics, or before closing a session mid-task.

---

## Tools reference

| Tool | Args | Description |
|---|---|---|
| `get_local_structure` | `path`, `max_depth=5` | Directory tree of a local folder |
| `get_github_structure` | `repo`, `branch="main"` | File tree of a GitHub repo (`owner/repo`) |
| `get_git_history` | `path`, `count=5` | Recent commits as `hash \| subject` |
| `save_memory` | `project_path`, `key`, `content` | Save or update a context entry |
| `load_memory` | `project_path` | Load all saved context (compressed) |
| `delete_memory` | `project_path`, `key` | Remove a specific entry |
| `set_compression` | `project_path`, `level` | Set compression level (0, 1, or 2) |
| `save_history` | `project_path`, `summary`, `session_id` | Save a conversation history chunk |
| `load_history` | `project_path`, `last_n=5` | Load recent conversation summaries |

---

## Optional — GitHub token for private repos

Without a token, GitHub API allows 60 requests/hour. For private repos or higher limits:

**Windows:**
```bash
$env:GITHUB_TOKEN = "your_token_here"
```

**Mac/Linux:**
```bash
export GITHUB_TOKEN="your_token_here"
```

---

Built with [FastMCP](https://gofastmcp.com).

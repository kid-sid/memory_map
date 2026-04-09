# Memory Map — File Structure MCP Server

An MCP server for Claude that provides project structure, git history, and persistent memory — so Claude understands your codebase from the first message of every session.

## What it does

- **Project structure** — local directory tree or any GitHub repo, `.gitignore`-aware
- **Git history** — recent commits in a compact format
- **Session memory** — save context once, Claude loads it automatically every session

## Prerequisites

- Python 3.10+
- Git installed and on your PATH
- [Claude Code](https://claude.ai/code) CLI installed

---

## Setup

### Step 1 — Clone and install dependencies

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

Note the full path to your `server.py` — you'll need it in the next step.
Example: `C:/Users/yourname/memory_map/server.py`

---

### Step 2 — Register the MCP server with Claude Code

Run this command once (replace the path with your actual path):

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

You should see `file-structure` listed with 6 tools. If it's not there, double-check the path in Step 2.

---

### Step 4 — Enable session memory for a project

Copy `CLAUDE.md` from this repo into the root of any project you want Claude to remember:

**Windows:**
```bash
copy C:\Users\yourname\memory_map\CLAUDE.md C:\Users\yourname\your-project\CLAUDE.md
```

**Mac/Linux:**
```bash
cp ~/memory_map/CLAUDE.md ~/your-project/CLAUDE.md
```

Claude Code reads `CLAUDE.md` automatically at session start and will call `load_memory` before doing anything else.

---

## How session memory works

**First session** — Claude explores your project and saves what matters:
```
save_memory(path, "stack", "Next.js frontend, FastAPI backend, Postgres")
save_memory(path, "entry_point", "main.py bootstraps the app, loads config from .env")
save_memory(path, "gotchas", "never import db.py directly, always go through models/")
```

**Every session after** — Claude calls `load_memory` first and has full context instantly:
```
stack: Next.js frontend, FastAPI backend, Postgres
entry_point: main.py bootstraps the app, loads config from .env
gotchas: never import db.py directly, always go through models/
```

No file reading. No re-explaining the codebase. Context in one tool call.

---

## Tools reference

| Tool | Args | Description |
|---|---|---|
| `get_local_structure` | `path`, `max_depth=5` | Directory tree of a local folder |
| `get_github_structure` | `repo`, `branch="main"` | File tree of a GitHub repo (`owner/repo`) |
| `get_git_history` | `path`, `count=5` | Recent commits as `hash \| subject` |
| `save_memory` | `project_path`, `key`, `content` | Save a context entry |
| `load_memory` | `project_path` | Load all saved context |
| `delete_memory` | `project_path`, `key` | Remove a specific entry |

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

To make it permanent, add the export to your shell profile (`.bashrc`, `.zshrc`, etc.).

---

Built with [FastMCP](https://gofastmcp.com).

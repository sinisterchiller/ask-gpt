# How to Configure the ChatGPT Bridge MCP with Claude Code

## Problem

When running `claude mcp list` from a project that doesn't contain the bridge server, you get:

```
chatgpt-bridge: ... ✘ Failed to connect — CONNECTION_CLOSED: Connection closed
```

## Root Cause

Claude Code **ignores the `cwd` field** in `.mcp.json`. When it launches `python -m server` from the project directory (e.g., `portfolio/`), Python can't find the `server` package because it lives in `ask-gpt/`. Python exits immediately, stdio closes, and Claude reports CONNECTION_CLOSED.

## Solution: User-Scoped MCP with Location-Independent Launcher

### Step 1 — Create a Launcher Script

Create a script in the bridge project that changes to the right directory before launching:

```bash
# ask-gpt/run_mcp.sh
#!/usr/bin/env bash
set -euo pipefail
cd /Users/anuragkoushik/Desktop/Proj/ask-gpt
export PYTHONUNBUFFERED=1
exec /Users/anuragkoushik/Desktop/Proj/ask-gpt/.venv/bin/python -m server
```

Make it executable:

```bash
chmod +x run_mcp.sh
```

### Step 2 — Add User-Scoped MCP Config

Run this from any directory:

```bash
claude mcp add --transport stdio --scope user chatgpt-bridge -- /Users/anuragkoushik/Desktop/Proj/ask-gpt/run_mcp.sh
```

This stores the config in `~/.claude.json` at the top level, making it available in **every** project.

### Step 3 — Remove Project-Scoped Definitions

If you previously added `chatgpt-bridge` to any project's `.mcp.json`, remove it. Project-scoped definitions shadow user-scoped ones:

```bash
# From each project that has chatgpt-bridge:
claude mcp remove chatgpt-bridge -s project
```

Or manually remove the `"chatgpt-bridge"` entry from the project's `.mcp.json` and `.claude/settings.local.json`.

### Step 4 — Verify

From **any** directory:

```bash
cd /path/to/any/project
claude mcp list
```

Expected output:

```
chatgpt-bridge: /Users/.../run_mcp.sh  - ✔ Connected
```

Not:

```
chatgpt-bridge: ... ✘ Failed to connect — CONNECTION_CLOSED
```

## Why This Works

| What | Why it matters |
|------|----------------|
| `--scope user` | Config lives in `~/.claude.json`, available in all projects |
| `run_mcp.sh` | Hardcodes `cd` + `exec`, so it works regardless of Claude's cwd |
| No `cwd` field | Claude Code ignores it — don't rely on it |
| No project `.mcp.json` | Project scope shadows user scope — remove duplicates |

## Precedence Rules

Claude Code resolves MCP servers by precedence (highest first):

1. **Local** — project-specific override in `~/.claude.json`
2. **Project** — `.mcp.json` in the workspace root
3. **User** — `~/.claude.json` top-level `mcpServers`

If the same name exists at multiple scopes, the highest precedence wins. That's why project-scoped `chatgpt-bridge` entries must be removed for the user-scoped one to take effect.

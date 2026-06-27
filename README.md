# diary-mcp

MCP server for Claude — persistent memory tree and project diary, backed by PostgreSQL.

Provides Claude with a structured, searchable, cross-session memory system and a project journal (log entries, milestones, tasks, wiki pages, reminders, error solutions). Supports bidirectional sync between machines via SSH tunnel.

## Features

- **Memory tree** — hierarchical key-value store (`/user/...`, `/feedback/...`, `/projects/<slug>/...`, `/references/...`) with importance scoring, decay via `valid_until`, and a knowledge-graph layer (links: related/supports/contradicts/requires/derived_from)
- **Two-tier model** — `curated` (consciously saved, always searched) vs. `extracted` (auto-captured from session transcripts, not searched by default to keep costs low)
- **Semantic search** — multilingual embeddings via fastembed (`paraphrase-multilingual-MiniLM-L12-v2`, 384d, CPU-only). pgvector + HNSW where available, numpy brute-force fallback
- **Hybrid search** — FTS ∪ semantic via Reciprocal Rank Fusion, ranked by importance + recency
- **Session hooks** — SessionStart injects project-scoped and global memories; SessionEnd optionally extracts structured memories from the conversation (per-project opt-in, off by default)
- **Project diary** — log entries, milestones, tasks, wiki pages, reminders, error/solution pairs per project
- **Web UI** — read-only FastAPI dashboard on `localhost:8765` (`diary-web`)
- **Bidirectional sync** — `memory_sync()` opens an ephemeral SSH tunnel and syncs last-write-wins, including soft-deleted tombstones and embeddings

## Requirements

- Python ≥ 3.11
- PostgreSQL 15+ (local: `diary_mcp` database with trust auth)
- Optional: [pgvector](https://github.com/pgvector/pgvector) extension for HNSW-accelerated semantic search

## Installation

```bash
uv tool install .
```

This installs two CLI entry points:

| Command | Description |
|---------|-------------|
| `diary-mcp` | MCP server (stdio transport) |
| `diary-web` | Local web dashboard on port 8765 |

### Database setup

```bash
createdb diary_mcp
```

Tables are created automatically on first start.

### Claude Code integration

Add to your `.mcp.json`:

```json
{
  "mcpServers": {
    "diary": {
      "command": "diary-mcp",
      "env": {
        "DIARY_DATABASE_URL": "postgresql://localhost/diary_mcp"
      }
    }
  }
}
```

For cross-machine sync, also set:

```
DIARY_REMOTE_SSH_HOST=user@host
DIARY_REMOTE_URL=postgresql://localhost:54321/diary_mcp
```

### Session hooks

Register in `settings.json` to auto-inject memories on session start and capture extractions on end:

```json
{
  "hooks": {
    "PreToolUse": [],
    "UserPromptSubmit": [{ "command": "python3 ~/.claude/hooks/diary_session_start.py" }],
    "Stop": [{ "command": "python3 ~/.claude/hooks/diary_session_end.py" }]
  }
}
```

## Memory tools

| Tool | Purpose |
|------|---------|
| `memory_upsert(path, title, body, type, ...)` | Save or update a memory |
| `memory_get(path)` | Read a node, tracks access |
| `memory_search(query)` | Lexical FTS with ILIKE fallback |
| `memory_search_semantic(query)` | Semantic search (meaning, cross-lingual) |
| `memory_context()` | Session start: tree + recently changed nodes |
| `memory_project_context(slug)` | Load auto-inject memories for a project |
| `memory_tree(path)` | Compact hierarchy from a given path |
| `memory_pin(path, on_start, on_compact)` | Mark memory for auto-injection |
| `memory_sync()` | Bidirectional sync via SSH tunnel |
| `memory_health()` | Report expired, empty, or contradicting nodes |
| `memory_promote(path)` | Promote extracted memory to curated |
| `memory_purge_tombstones(older_than_days)` | Clean up soft-deleted nodes |

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DIARY_DATABASE_URL` | `postgresql://localhost/diary_mcp` | Local PostgreSQL connection |
| `DIARY_REMOTE_SSH_HOST` | — | SSH host for sync |
| `DIARY_REMOTE_URL` | — | Remote PostgreSQL URL (via tunnel) |
| `DIARY_EMBED_MODEL` | `paraphrase-multilingual-MiniLM-L12-v2` | fastembed model name |
| `DIARY_AUTO_EXTRACT` | `0` | Global override to enable session extraction |

## Development

```bash
# Install with dev dependencies
uv sync --extra dev

# Run tests (requires diary_mcp_pytest and diary_mcp_pytest_remote databases)
createdb diary_mcp_pytest && createdb diary_mcp_pytest_remote
uv run pytest
```

44 tests cover upsert/update, access tracking, tombstones, two-tier visibility, hybrid search, embedding fallback, sync round-trip, extracted lifecycle, and project config.

CI runs on GitHub Actions with a pgvector service container.

## Architecture

```
diary_server.py   — FastMCP stdio server (22 memory_* tools + diary tools)
diary_db.py       — PostgreSQL access layer (psycopg3)
diary_embed.py    — fastembed wrapper, lazy-load, pgvector-aware
diary_web.py      — FastAPI read-only dashboard
diary_config.py   — env config
```

Memory nodes are stored with UUID primary keys (conflict-free sync), `path` unique index, `importance` (0–1), `access_count`/`accessed_at`, `valid_until`, `origin` (curated|extracted), and a 384-dimensional embedding column. The knowledge graph lives in a `memory_links` table.

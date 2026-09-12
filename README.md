# diary-mcp

MCP server for Claude — persistent memory tree and project diary, backed by PostgreSQL.

Provides Claude with a structured, searchable, cross-session memory system and a project journal (log entries, milestones, tasks, wiki pages, reminders, error solutions). Supports bidirectional sync between machines via SSH tunnel.

## Features

- **Memory tree** — hierarchical key-value store (`/user/...`, `/feedback/...`, `/projects/<slug>/...`, `/references/...`) with importance scoring, decay via `valid_until`, and a knowledge-graph layer (links: related/supports/contradicts/requires/derived_from)
- **Two-tier model** — `curated` (consciously saved, always searched) vs. `extracted` (auto-captured from session transcripts, not searched by default to keep costs low). A similarity **tripwire** (cosine ≥ 0.85) still surfaces near-duplicate extracted hits as a "see also" hint even in default search, so the tier never becomes fully write-only.
- **Semantic search** — multilingual embeddings via fastembed (`paraphrase-multilingual-MiniLM-L12-v2`, 384d, CPU-only). pgvector + HNSW where available, numpy brute-force fallback
- **Hybrid search** — FTS ∪ semantic via Reciprocal Rank Fusion, ranked by importance + recency. `contradicts` links on a matched node are surfaced inline (also on `memory_get`), instead of only being visible via a manual `memory_health()` run.
- **Session hooks** — SessionStart injects project-scoped and global memories; UserPromptSubmit runs an automatic FTS-only per-turn retrieval against curated memories (deterministic, no embeddings — see Architecture); SessionEnd optionally extracts structured memories from the conversation (per-project opt-in, off by default)
- **Project diary** — log entries, milestones, tasks, wiki pages, reminders, error/solution pairs per project
- **Web UI** — read-only FastAPI dashboard on `localhost:8765` (`diary-web`)
- **Bidirectional sync** — `memory_sync()` opens an ephemeral SSH tunnel and syncs last-write-wins, including soft-deleted tombstones and embeddings
- **Two entry points** — `diary-mcp` (day-to-day tool surface) and `diary-admin-mcp` (knowledge-graph introspection/maintenance: explain/path/stats/infer/report), kept separate so an ordinary session isn't shown audit-only tools it never needs

## Requirements

- Python ≥ 3.11
- PostgreSQL 15+ (local: `diary_mcp` database with trust auth)
- Optional: [pgvector](https://github.com/pgvector/pgvector) extension for HNSW-accelerated semantic search

## Installation

```bash
uv tool install .
```

This installs three CLI entry points:

| Command | Description |
|---------|-------------|
| `diary-mcp` | Main MCP server (stdio transport) — day-to-day tool surface |
| `diary-admin-mcp` | Admin MCP server (stdio transport) — knowledge-graph introspection/maintenance tools, add only when doing an audit |
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

Add `diary-admin-mcp` the same way, only for the duration of a graph audit:

```json
{
  "mcpServers": {
    "diary-admin": {
      "command": "diary-admin-mcp",
      "env": { "DIARY_DATABASE_URL": "postgresql://localhost/diary_mcp" }
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

Register in `settings.json` (see `~/.claude/hooks/` for the actual scripts):

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/diary_session_start.py" }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/diary_prompt_retrieval.py" }] }],
    "SessionEnd": [{ "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/diary_session_end.py" }] }]
  }
}
```

- **SessionStart** injects memories pinned (`pin_triggers`) for `start`/`compact`, scoped to the project (via `cwd` → slug resolution) plus global `/user`, `/feedback`.
- **UserPromptSubmit** (`diary_prompt_retrieval.py`) runs the current prompt through Postgres FTS (the same tsvector index `memory_search` uses) against curated memories, scoped the same way, and silently injects the top matches. Deliberately FTS-only, not semantic — loading the embedding model fresh on every single prompt would reintroduce the cold-start stall documented in `Project.md`'s v0.8.1 postmortem. This replaced the old `trigger_keywords`/`memory_set_keywords` mechanism (retired in v0.10.0), which required maintaining an exact keyword list per memory by hand.
- **SessionEnd** optionally extracts structured memories from the conversation (per-project opt-in via `memory_set_project_config`, off by default).

## Automatic linking (v0.13.0)

Two complementary mechanisms keep the knowledge graph populated without a manual `memory_infer_links()` call:

- **Write-time (`memory_upsert`):** every curated save/edit compares the node's just-computed embedding against all other curated, embedded nodes and inserts `related`/`inferred` links above `AUTO_LINK_THRESHOLD` (0.82, `memory_service.py`) — capped at `AUTO_LINK_MAX_NEW` (3) per upsert to avoid graph spam. Nearly free (the embedding is already computed for the save itself); immediate.
- **Periodic batch (`scripts/link_inference_cron.py`):** re-runs `memory_infer_links()` over the whole curated tree — catches pairs the per-upsert pass can't (e.g. after `memory_reembed_all`, or ones that exceeded the per-upsert cap). NOT a Claude Code hook — a plain script for a scheduler, run with the diary-mcp tool's own installed Python so `import graph_admin` resolves:

  ```
  DIARY_LINK_INFERENCE_THRESHOLD=0.82 DIARY_LINK_INFERENCE_MAX_NEW=50 \
    ~/.local/share/uv/tools/diary-mcp/bin/python scripts/link_inference_cron.py
  ```

  Deployed locally as a systemd user timer (daily, 04:30): `~/.config/systemd/user/diary-link-inference.{service,timer}`, enabled via `systemctl --user enable --now diary-link-inference.timer`. Output/errors log to `~/.local/share/diary-link-inference.log`.

## Backup export (v0.14.0)

`scripts/memory_backup_export.py` mirrors every curated node as a Markdown file (frontmatter: title, type, tags, importance, valid_until, pin_triggers, updated_at) into a separate, private git repo — [diary-mcp-backup](https://github.com/Raindancer118/diary-mcp-backup) — giving a human-readable, diffable point-in-time backup independent of Postgres. Wipes and rewrites the whole `memory-tree/` directory each run so deletions show up as real diffs; commits + pushes only when something changed.

```
BACKUP_REPO_DIR=~/Projekte/SEProjects/diary-mcp-backup \
  ~/.local/share/uv/tools/diary-mcp/bin/python scripts/memory_backup_export.py
```

Deployed locally as a systemd user timer (daily, 04:00, i.e. before the link-inference timer): `~/.config/systemd/user/diary-memory-backup.{service,timer}`. Log: `~/.local/share/diary-memory-backup.log`.

## Tag lookup

`memory_list_by_tag(tag, include_extracted=False)` — the `tags` column (settable since the original schema via `memory_upsert(tags=...)`) was write-only until v0.14.0; this queries curated nodes by exact tag match.

## Diary federation (v0.15.0)

E2EE pairing/sync with another person's diary-mcp instance via the separate
[diary-relay](https://github.com/Raindancer118/diary-relay) service — see
that repo for the server side and full trust model. The relay only ever
sees ciphertext and public keys, never private keys or plaintext; encryption
uses PyNaCl `Box` (X25519 + XSalsa20-Poly1305), one long-lived keypair per
diary. Only a tag-scoped subset of curated memories is ever shared.

```
diary_link_init("Alice", "https://diary-relay.example")   # once, generates the keypair
diary_link_create_pairing_code()                          # share the code out-of-band
diary_link_check_pairing_code(code, "bob")                # poll until the other side redeems it
# ...meanwhile the other person runs diary_link_redeem_pairing_code(code, "alice")...
diary_link_sync("bob", "share-with-bob")                  # push tagged nodes, pull + decrypt theirs
diary_link_list()
diary_link_unlink("bob")
```

Incoming synced content lands under `/links/<alias>/<original-path>`, tagged
`from:<alias>` — it never overwrites your own tree. Manual, explicit sync
calls only in this phase (no automatic background sync). Verified end-to-end
against a real running diary-relay with two separate diary-mcp processes
(not just mocked tests) during development.

**Live at https://diary-relay.tstieh.de** (deployed 12.09.2026) — pass this
URL as `relay_url` to `diary_link_init`.

## Memory tools

| Tool | Purpose |
|------|---------|
| `memory_upsert(path, title, body, type, ...)` | Save or update a memory |
| `memory_get(path)` | Read a node, tracks access, surfaces `contradicts` warnings |
| `memory_search(query)` | Hybrid FTS+semantic search (RRF), extracted-tier tripwire, contradiction warnings |
| `memory_recall(query, top_k)` | One-shot agentic recall: same hybrid search, compacted to top_k, plus 1-hop graph neighbors + contradiction warnings in a single call |
| `memory_search_semantic(query)` | Semantic search (meaning, cross-lingual) |
| `memory_context()` | Session start: tree + recently changed nodes |
| `memory_project_context(slug)` | Load auto-inject memories for a project |
| `memory_tree(path)` | Compact hierarchy from a given path |
| `memory_list_by_tag(tag)` | Exact-match lookup of curated memories by tag |
| `memory_pin(path, on_start, on_compact)` | Mark memory for auto-injection |
| `memory_link(from_path, to_path, rel_type)` | Create a knowledge-graph link |
| `memory_sync()` | Bidirectional sync via SSH tunnel |
| `memory_promote(path)` | Promote extracted memory to curated |
| `memory_merge(keep_path, merge_path)` | Fold a curated duplicate/near-duplicate into another node (body append, embedding refresh, link repoint), then tombstone it |
| `memory_purge_tombstones(older_than_days)` | Clean up soft-deleted nodes |

Knowledge-graph introspection/maintenance tools (`memory_explain`, `memory_path`, `memory_graph_stats`, `memory_infer_links`, `memory_query_graph`, `memory_report`, `memory_consolidate_report`) live on the separate `diary-admin-mcp` entry point, not on the main server — see Architecture. `memory_consolidate_report` surfaces near-duplicate curated pairs (merge candidates for `memory_merge`) and stale, low-importance, unpinned nodes (archive/prune candidates) — a read-only report, no automatic deletion or merging.

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

98 tests cover upsert/update, access tracking, tombstones, two-tier visibility (incl. the extracted-tier tripwire), hybrid search, contradiction surfacing, embedding fallback, sync round-trip, extracted lifecycle, and project config.

CI runs on GitHub Actions with a pgvector service container.

## Architecture

```
diary_server.py          — main FastMCP stdio server: bootstrap + re-exports (thin by design)
diary_bootstrap.py        — shared `mcp` FastMCP instance for the main server
diary_project_tools.py    — projects/logs/errors/milestones/tasks/reminders/wiki tools
memory_service.py         — memory tree CRUD, two-tier lifecycle, pinning, contradiction lookup
search_engine.py          — hybrid search (FTS+semantic, RRF), ranking, extracted-tier tripwire
graph_core.py             — memory_link / memory_get_links (kept on the main server)
sync_manager.py           — memory_sync / memory_sync_diary / tombstone purges

diary_admin_server.py     — separate diary-admin-mcp entry point
diary_admin_bootstrap.py  — its own `admin_mcp` FastMCP instance
graph_admin.py            — knowledge-graph introspection/maintenance tools (explain/path/
                             stats/infer/report) — admin-only, not on the main server's tool
                             surface (see module docstring for the 2026-08 rationale)

diary_db.py       — PostgreSQL access layer (psycopg3)
diary_embed.py    — fastembed wrapper, lazy-load, pgvector-aware
diary_web.py      — FastAPI read-only dashboard
diary_config.py   — env config
```

The tool-implementation modules import `diary_db`/`diary_embed` as modules (`diary_db.get_db()`,
not `from diary_db import get_db`) rather than importing individual functions by name — the test
suite reloads `diary_db`/`diary_embed` between DB targets, and a `from...import` binding would
keep pointing at the stale pre-reload function object.

Memory nodes are stored with UUID primary keys (conflict-free sync), `path` unique index, `importance` (0–1), `access_count`/`accessed_at`, `valid_until`, `origin` (curated|extracted), `trigger_keywords` (retired in v0.10.0, column kept for schema stability — no destructive migration on the live synced DB), and a 384-dimensional embedding column. The knowledge graph lives in a `memory_links` table.

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
- **Web UI** — „Observatorium“ on `localhost:8765` (`diary-web`): memory archive, knowledge graph as a star map, statistics dashboard
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

`uv tool install .` also installs `diary-hook`. Register it in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "SessionStart": [{ "hooks": [{ "type": "command", "command": "diary-hook session-start", "timeout": 10 }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "diary-hook prompt", "timeout": 10 }] }],
    "SessionEnd": [{ "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/diary_session_end.py" }] }]
  },
  "autoMemoryEnabled": false
}
```

`autoMemoryEnabled: false` turns off Claude Code's built-in file memory, whose system-prompt section otherwise contradicts "diary is the memory".

- **SessionStart** (`startup`/`resume`/`clear`/`compact`) stays lean: project pins in full plus one line saying how many memories the project has and how to fetch them (`memory_project_context(slug)` for the ranked index, `memory_recall(query)` for search). Nothing else is preloaded; Claude pulls context when a session needs it. Project = cwd via `memory_set_project_dir` aliases, else basename.
- **UserPromptSubmit** injects at most 2 curated memories, and only with confidence ≥ 90 % and importance ≥ 0.5. Lexical confidence = min(share of the prompt's idf-weighted terms the memory contains, 0.5 + share found in its title). The title part stops long logs that mention everything from winning. Semantic confidence maps cosine similarity 0.50 → 0 and 0.80 → 1. Both signals are combined as 1 − (1−a)(1−b). Semantic matching only runs if a diary-mcp process already runs the shared embedding server; the hook never loads a model. Conversational filler (halt, gern, dass …) is ignored, each memory is injected once per session (reset on compaction), and trivial prompts are skipped.
- **SessionEnd** optionally extracts structured memories from the conversation (per-project opt-in via `memory_set_project_config`, off by default).

Pins (`memory_pin`) are only allowed inside a project (`/projects/<slug>/…`); global rules reach the model as one-liners in the digest instead. Every injected pin block carries the instruction to keep pins relevant and `memory_unpin` the ones without value.

### Web UI (v0.23.0)

`diary-web` serves three views, all offline (fonts are bundled, nothing is fetched from the internet):

- **Archiv** (`#/`, `#/m/<path>`) — tree with path filter, rendered Markdown bodies (memory paths and `[[slug]]` links become clickable), metadata, incoming/outgoing links as a small constellation.
- **Sternkarte** (`#/karte`) — knowledge graph; linked memories cluster per project, unlinked ones form the outer field. Hover highlights neighbours, click opens.
- **Messwerte** (`#/messwerte`) — everything from `/api/stats` plus a one-year activity heatmap from `GET /api/activity?days=365` (per day: created, last-edited, project log entries).

Keys: `Ctrl/⌘ K` or `/` search, `1`/`2`/`3` switch views, `Esc` closes. Assets live in `diary_web_assets/` (package data); design decisions in `Design.md`. Honours `prefers-reduced-motion`; strict CSP, no framing.

### Web UI autostart (v0.22.0)

Every diary-mcp server checks at startup whether diary-web is listening on `127.0.0.1:8765` (`DIARY_WEB_PORT`). If it isn't, the server starts diary-web detached (it outlives the MCP process; log: `~/.cache/diary-mcp/diary-web.log`) and opens it in the browser. An already running UI is left alone, so new Claude sessions don't open more tabs. A file lock ensures only one of several concurrently starting sessions does this. Skipped without `WAYLAND_DISPLAY`/`DISPLAY`, over SSH (e.g. the Dorn instance) and with `DIARY_WEB_AUTOSTART=0`.

### Memory style

Memories should be small and specific: one fact per memory, no filler, bullet points; they only need to be readable for Claude. `memory_upsert` still saves bodies over 1200 characters but asks to split them, and `memory_stats` counts them as cleanup candidates.

### Statistics (v0.19.0)

`memory_stats(days=30, project_slug="")` (MCP tool) and `GET /api/stats?days=30&project=<slug>` (diary-web, JSON) report:

- **Corpus:** curated/extracted memories, approximate tokens, projects, embeddings, links, pins.
- **Injection efficiency:** from the hook event log (`~/.cache/diary-mcp/injection_log.jsonl`, paths and sizes only, capped at 2 MB): sessions, prompts, hit rate, semantic share, average tokens per digest, per hit prompt and per session, latency, most-injected memories.
- **File-based comparison:** Claude Code's `~/.claude/projects/*/memory` files (count, tokens, how many already exist in the diary). With `project_slug` it also compares the tokens loaded per session and the knowledge each system can reach.

Token counts are estimates (characters / 3.7).

## Automatic linking (v0.24.0)

Every candidate pair of curated memories gets a **confidence** (`link_inference.py`):

| Signal | Effect |
|---|---|
| Text mentions the other memory (path or `[[slug]]`) | confidence 0.95 |
| Near-identical content (cosine ≥ 0.9) | at least 0.9 |
| Otherwise a logistic model over hub-corrected embedding similarity, mutual neighbour rank, shared rare terms (idf), same project, same folder and shared graph neighbours | 0 … 1 |

The model is refitted nightly on the **deliberate** links only (set by hand or approved suggestions), with non-negative weights; automatic links never train the next round. Below 40 deliberate links it uses built-in default weights.

- **≥ 0.7:** linked automatically (`link_origin = 'inferred'`, with `confidence` and a human-readable `evidence`).
- **0.35–0.7:** stored in `link_suggestions`. This list is **only processed on explicit request** via `memory_link_suggestions()` and `memory_link_suggestions_decide(ids, 'approve'|'reject')`. Approved pairs become deliberate links; rejected pairs are never suggested or linked again.
- Hand-set links are never touched; existing automatic links are re-scored, never deleted.

Runs at write time (`memory_upsert`, ~65 ms with a warm per-process cache, at most `AUTO_LINK_MAX_NEW` non-mention links per save) and nightly over the whole tree (`scripts/link_inference_cron.py`, systemd user timer `diary-link-inference.timer`, 04:30, log `~/.local/share/diary-link-inference.log`). `memory_stats` and diary-web show auto-link and suggestion counts. The admin tool `memory_infer_links(threshold)` is the older pure-cosine variant for manual use.

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
`from:<alias>` — it never overwrites your own tree. `diary_link_sync()`
itself stays a manual, explicit tool call. Verified end-to-end against a real
running diary-relay with two separate diary-mcp processes (not just mocked
tests) during development.

**Live at https://diary-relay.tstieh.de** (deployed 12.09.2026) — pass this
URL as `relay_url` to `diary_link_init`.

### Automatic sync (v0.16.0)

Opt-in, on top of the manual tool — same two-mechanism pattern as
[Automatic linking](#automatic-linking-v0240) (write-time + periodic batch):

```
diary_link_set_sync_tags("bob", "share-with-bob,recipes")   # comma-separated; "" disables
```

- **Write-time push (`diary_link.push_node_on_upsert`):** every `memory_upsert()`
  of a curated node whose tags overlap a link's `sync_tags` immediately
  encrypts and pushes that ONE node to the relay for that link — "on change",
  no waiting for a schedule. Runs after `memory_upsert`'s own DB transaction is
  closed (a network call must never sit inside an open transaction, see
  `Project.md`'s v0.8.1 postmortem). Never raises: a missing identity, no
  matching link, or an unreachable relay just means "not pushed", never a
  failed save.
- **Periodic batch (`scripts/diary_link_sync_cron.py`):** reads every link's
  `sync_tags` (empty by default — a link stays untouched unless explicitly
  opted in) and calls `diary_link_sync(alias, tag)` for each configured pair —
  this is what actually **pulls** the peer's messages, and catches anything
  the write-time push missed (e.g. a relay outage at save time). Its push side
  is a **delta** (`updated_at > last_synced_at`), not a full resend of the
  whole tag scope every night — otherwise a nightly run would recreate one
  fresh relay message per tagged node forever, even for content that never
  changed. `diary_link_set_sync_tags()` separately triggers a one-time full
  catch-up push when a tag is newly enabled, so pre-existing tagged nodes
  aren't stranded waiting for an edit. One failing (link, tag) pair is logged
  and skipped, not fatal to the rest of the run. Not a Claude Code hook — a
  plain script for a scheduler, run with the diary-mcp tool's own installed
  Python:

```
~/.local/share/uv/tools/diary-mcp/bin/python scripts/diary_link_sync_cron.py
```

Deployed locally as a systemd user timer (daily, 04:15, between the memory
backup and link-inference timers): `~/.config/systemd/user/diary-link-sync.{service,timer}`, enabled via `systemctl --user enable --now diary-link-sync.timer`. Log: `~/.local/share/diary-link-sync.log`.

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
diary_web.py      — FastAPI app for diary-web (JSON API + static assets)
diary_web_assets/ — index.html, app.css, app.js, bundled woff2 fonts
diary_config.py   — env config
```

The tool-implementation modules import `diary_db`/`diary_embed` as modules (`diary_db.get_db()`,
not `from diary_db import get_db`) rather than importing individual functions by name — the test
suite reloads `diary_db`/`diary_embed` between DB targets, and a `from...import` binding would
keep pointing at the stale pre-reload function object.

Memory nodes are stored with UUID primary keys (conflict-free sync), `path` unique index, `importance` (0–1), `access_count`/`accessed_at`, `valid_until`, `origin` (curated|extracted), `trigger_keywords` (retired in v0.10.0, column kept for schema stability — no destructive migration on the live synced DB), and a 384-dimensional embedding column. The knowledge graph lives in a `memory_links` table.

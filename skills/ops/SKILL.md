---
name: neroli-ops
description: Agent-native operations guide for maintaining Neroli. Use it to route maintenance, tuning, troubleshooting, and module-specific edits to the smallest relevant ops note.
---

# Neroli OPS — instructions for the maintaining agent

You are a coding agent maintaining an existing Neroli install. Start here when the user asks to run the pipeline, tune behavior, troubleshoot output, or change a module.

## Rules

1. Read only the smallest relevant module note first; do not load the whole ops set unless the task crosses module boundaries.
2. For passwords, API keys, and authorization prompts, stop and call the user instead of trying increasingly indirect workarounds.
3. Do not call Claude or any other paid model/agentic CLI unless the user has explicitly allowed that action for this task.
4. When a deterministic behavior or file layout changes, update the corresponding documentation in the same pass.

## Route by module

| Module | Read | Command keywords |
|---|---|---|
| Ingest | [ingest.md](ingest.md) | `--ingest-only`, Claude.ai import |
| Card Gen | [card-gen.md](card-gen.md) | `--process-existing`, `--auto-cards`, models, prompts |
| Index | [index.md](index.md) | `--rebuild-index`, Leiden, embeddings |
| Context | [context.md](context.md) | `--rebuild-context`, last-24 |
| Search | [search.md](search.md) | `src/retrieval.py`, retrieval |
| Midlayer | [midlayer.md](midlayer.md) | `--curate-snapshot`, `--curate-dry-run`, `--curate-export-workbench`, tree snapshots, per-room switches |
| Cross-module / runtime | [common.md](common.md) | launchd, migrations, backups, file layout |
| Schema | [../../schema.md](../../schema.md) | messages / turns / cards / clusters |

## What to edit

| Change | Edit | Ops note |
|---|---|---|
| Card prompt | `prompts/gen-cards-prompt.md` inside the code block | [card-gen.md](card-gen.md) |
| Agent persona | `prompts/agent-persona-<room>.md` | [card-gen.md](card-gen.md) |
| Night curator prompt / room persona | `prompts/night-curator.md` + `prompts/agent-persona-<room>.md` | [midlayer.md](midlayer.md) |
| Thresholds, clustering, context parameters | `config/settings.json` | Relevant module note |
| Add a room | `config/rooms.json` + kickstart watcher | [common.md](common.md) |
| Card-generation logic | `src/gen_cards.py` | [card-gen.md](card-gen.md) |
| Clustering / index behavior | `src/graph.py`, `src/community.py`, etc. | [index.md](index.md) |
| Search behavior | `src/retrieval.py` | [search.md](search.md) |
| `context-last-24.md` rebuild behavior | `src/context.py` | [context.md](context.md) |
| Night tree snapshots, digests, constants, per-room switches | `src/treesnap.py`, `src/curator.py`, `config/settings.json` -> `midlayer` | [midlayer.md](midlayer.md) |
| SQLite connections, ingest writes, FTS, audit, visibility predicate | `src/db.py` | [common.md](common.md) |

For the module boundary map, read [../../ARCHITECTURE.md](../../ARCHITECTURE.md). For schema details, read [../../schema.md](../../schema.md).

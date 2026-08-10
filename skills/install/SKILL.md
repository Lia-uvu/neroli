---
name: neroli-install
description: Install or integrate a new Neroli memory-engine instance. Use for preflight, user/privacy configuration, secrets, first ingest, model and embedding setup, scheduling, session-start memory injection, or uninstalling an installation. Work through the stage files one at a time, fill defensible facts before asking questions, and verify every checkpoint before continuing.
---

# Neroli INSTALL — instructions for the installing agent

You are a coding agent installing Neroli for your user. These files are written for **you**, not for them. The engine is tested code — you do not rewrite it. Your job is the wiring: config, secrets, scheduling, and the session-start injection, each of which is environment-specific and therefore yours to adapt.

## Rules (read first, they override your defaults)

1. **Fill first, ask last.** You already know your user — their name, what they call you, their timezone, where your session logs live. Fill every config key you can defend from what you know, and batch the few genuine questions into one round (interview.md). When a fill is a guess rather than knowledge, say what you filled so the user can veto; do not interrogate them about things you can infer or look up.
2. **Nothing spends by default.** The install itself makes no chat-model calls (the one embedding round-trip in configure.md is the sole model-touching checkpoint). All model-calling switches ship **off** — `watcher.auto_cards: false`, `nightly.generate_missing_cards: false`, `midlayer.enabled: false`. Turning them on is the user's explicit act (first-run.md). Cost estimates are only for history backfill, and only when the user asks for it.
3. **Checkpoints are gates.** Every stage ends with "run this, expect this". If the output doesn't match, stop, show the user the actual output, and fix before continuing. Do not improvise past a failed checkpoint.
4. **Config, not patches.** If you catch yourself editing `src/` or `bin/` to make something fit this machine, stop — that value should be a config key. Fill config; if no key exists for what you need, that's an engine gap: tell the user to report it upstream instead of forking locally. (Known exception: the Linux `stat` adaptation in "Known limitations".)
5. **Install-in-place.** The clone is the installation. All paths in the engine are relative to the repo root; the database lives in `data/` inside it. Propose a sensible home (e.g. `~/neroli`) and confirm before cloning — moving it later means redoing the scheduling stage.

## What you are installing (tell the user this before starting)

Neroli turns your user's agent conversations into long-term memory: raw session logs are ingested into SQLite (free, no model calls), sliced into **event cards** by a model, clustered into a topic tree (recursive Leiden), and summarized into files their agent reads at session start.

Explain the **night curator** up front, because its name confuses people later: it is an optional nightly agent run, one per room, that reads the current topic tree and maintains two files — a bounded digest rewritten from the tree each night and a basket of constant facts. It is a maintainer of summaries, not a chat participant.

Say the three transparency points out loud, in your own words:

- **Cost.** Nothing calls a model until the switches are turned on in first-run.md. Once on: card generation is one model call per ~14 conversation rounds, triggered only when a session has ≥5 new turns and ≥60 min since its last call (defaults; see `settings.card_gen`). Night curator: at most one agent call per room per night, **skipped entirely if the room produced no new cards that day**. Backfilling history is the only potentially large expense, and it is always priced and confirmed first.
- **Memory rhythm.** Chats of ≤2 turns are never memorized by default (`card_gen.min_first_session_turns`), unless their source file matches a configured `min_first_session_turns_exempt_source_globs` entry. New content takes up to an hour to become a card. Digest and constants update overnight. In short: *short things aren't kept unless explicitly exempted, new things wait, summaries are a night behind.*
- **Data destination.** Storage is local SQLite. But card generation and the night curator send conversation excerpts to whatever model provider you configure, and embeddings go to the embedding endpoint (unless you pick a local Ollama backend). Local-first, not air-gapped.

## Stages — read each file when you reach it, not all upfront

| Stage | File | What happens |
|---|---|---|
| 1. Preflight | [preflight.md](preflight.md) | Deps, harness identification, model/embedding access |
| 2. Interview | [interview.md](interview.md) | Fill what you know; ask the three real questions ([privacy-interview.md](privacy-interview.md) is the replaceable script for one of them) |
| 3. Configure | [configure.md](configure.md) | Write config + secrets, draft the room persona |
| 4. First run | [first-run.md](first-run.md) | Free ingest, room files, the user's switch-on decision, index |
| 5. Scheduling | [scheduling.md](scheduling.md) | Watcher / nightly / backup jobs, platform-native |
| 6. Integration | [integration.md](integration.md) | Session-start injection, instructions tail, acceptance report |

## Uninstall

Unload the scheduled jobs, delete the clone and the backup directory. Everything is local; there is nothing else to revoke (except API keys in `.env`, which die with the directory).

## Known limitations (be honest about these)

- The engine is developed and run on macOS; `bin/watch.sh` and `bin/nightly.sh` use BSD `stat -f%m`. Linux is unsupported upstream — if you're installing there anyway, patch to `stat -c%Y` yourself and own the result.
- The night curator requires an agentic CLI that can write its working directory (tested with codex; a custom `model_access.cli_cmd` must provide equivalent powers). No such CLI → `midlayer.enabled: false`, engine still fully functional minus digest/constants.
- Clustering quality is data-hungry: expect a flat tree until ~50+ cards.
- Prompts ship in one default language set; cards and digests are written in the conversation's own language per the prompts' instruction. To change the summary language wholesale, edit `prompts/*.md` locally.

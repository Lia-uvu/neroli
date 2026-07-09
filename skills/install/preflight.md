# Stage 1 — Preflight

```sh
pip install -r requirements.txt   # jieba, numpy, python-igraph, leidenalg (pinned)
bin/preflight.sh                  # checks python/sqlite/deps/fswatch/codex in one pass
```

`FAIL` lines must be fixed before continuing; `note` lines are context for the choices below, not errors.

**Identify the user's harness now** — it decides the integration stage's injection route: does its instructions file support `@file` includes (Claude Code's `CLAUDE.md` does)? Does it have session-start hooks? Note the answer; you'll use it later without asking the user.

Model access — one setting (`settings.model_access.provider`) covers the whole pipeline; the engine needs at least one of:

- `provider: "cli"` with `codex` on PATH (**the tested default** — the engine builds its own `codex exec` invocations, including the sandboxed one the night curator requires),
- `provider: "cli"` with any other headless CLI that reads a prompt on stdin and prints the reply — set `model_access.cli_cmd` as a template, e.g. `"claude -p --model {model}"` (`{model}` is filled per call site; the **night curator additionally needs the CLI to be agentic and able to write its working directory**),
- `provider: "api"`: an OpenAI-compatible chat API, keys in `.env` (works for cards, entity judge; the night curator still needs an agentic CLI — without one, leave `midlayer.enabled: false`; everything else works).

Embedding access (required — the index can't build without it): an OpenAI-compatible `/embeddings` endpoint serving `BAAI/bge-m3` or similar, **or** local Ollama with an embedding model pulled.

macOS realtime watcher (optional): `brew install fswatch`. Without it, use the periodic ingest in the scheduling stage.

**Checkpoint:** `bin/preflight.sh` exits 0 (no `FAIL` lines).

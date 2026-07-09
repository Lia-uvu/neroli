# Stage 2 — Fill what you know, then one round of questions

## Fill yourself (report the values, don't ask)

| Key | Fill from |
|---|---|
| `settings.user.name` / `.aliases` | What you already call the user (your instructions/memory). Ask **only** if you genuinely don't know. |
| `settings.agent.name` / `.aliases` | What the user calls you. Same rule. |
| `settings.timezone` | The machine's local timezone (`readlink /etc/localtime` or equivalent). Mismatch between local time and true timezone is the rare case — don't ask. |
| `config/rooms.json` | **One room, yours** (default). Tell the user: "memory is set up for me only; if you later want other agents with separate privacy scopes, that's a config addition." |
| `rooms[].project_dir` | Your own session logs. Claude Code: `~/.claude/projects/<encoded-cwd>/` (`<encoded-cwd>` = working directory with `/` → `-`). Verify it exists and contains `.jsonl` files. |
| `rooms[].room_dir` | Propose a sensible default (e.g. `<repo>/rooms/<room>/`); `mkdir -p` it. |
| `settings.backup.dir` / `.keep` | Default `<repo>/data/backups`, keep 14. |
| Schedule times | Nightly job 04:00, backup right after, ingest realtime (fswatch) or every 15 min. Tell the user the times; change only if they object. |
| Prompt set | Use the repository's shipped prompts as-is — no language question. The prompts instruct the model to write cards in the conversation's own language. |

## Ask the user (one batch, exactly these)

1. **Share/private boundary.** Run the interview in [privacy-interview.md](privacy-interview.md) — it is a **replaceable template**: adapt its wording to your user, and never present Neroli's examples as their policy. Answers land in the room persona (configure.md).
2. **Model access, one merged question:** how do you reach models — codex CLI (default), another CLI, or an API? Which model for cards? If you'll want the night curator, which model for it? And embeddings: remote API or local Ollama? → `settings.model_access.*`, `settings.card_gen.model`, `settings.midlayer.model`, `settings.embedding.*` (+ `.env` keys if API).
3. **Existing chat archives?** Any exported conversation files from **any** platform they'd like ingested as history. claude.ai web exports have a ready loader (`settings.ingest.origin_data_dir` + `scripts/ingest_claude_ai_exports.py`); other formats usually need a source-specific normalize/clean script plus a small loader. Read `skills/ops/ingest.md` and `schema.md`, then verify the cleaned sample before importing.

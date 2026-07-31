# Stage 6 — Session-start injection, instructions tail, acceptance

Two pieces, both adapted to the harness you identified in preflight.

## (a) Injection — preference order

1. **`@file` includes** (harness supports them in the instructions file, e.g. Claude Code's `CLAUDE.md`): reference the memory files directly — `@<room_dir>/digest.md`, `@<room_dir>/constants.md`. Simplest and survives harness updates.
2. **Session-start hook** (no `@file`, but hooks exist): emit only non-empty files — **empty state must not occupy a line:**

```sh
#!/usr/bin/env bash
# neroli session-start hook: emit memory files that have content
for f in summary-last-24.md digest.md constants.md kept.md todo.md; do
  p="<room_dir>/$f"
  [ -s "$p" ] && printf '\n== %s ==\n' "$f" && cat "$p"
done
exit 0
```

3. **Neither:** adapt to whatever the platform offers (startup prompt, MCP resource, wrapper script). The contract is only: the agent sees the non-empty memory files at session start.

## (b) Instructions tail

Append to the room agent's instructions file (e.g. `CLAUDE.md`), wrapped in markers — future upgrades replace *only* what's between them, so never put the user's own content inside. **Keep it minimal: it is injected into every session, every line is a running cost.** Its one irreplaceable job is handing the agent a working recall tool; one compact rhythm line spares the agent inventing excuses when asked "why don't you remember X". **Fill in the real repo root and room name** so the commands are copy-paste runnable:

```markdown
<!-- yard:tools:begin -->
## Memory (Neroli)
- Search long-term memory (from `<repo-root>`): `python3 src/retrieval.py --viewer <room> "<keyword>"` — also `--top` (topic tree), `--cluster <id>`, `--card <id>`, `--time <start> [<end>]`.
- Rhythm: short chats aren't kept, new content cards within ~1h, digest/constants update overnight — say so instead of guessing when asked why you don't remember something.
- not: don't edit cards-last-24.md / summary-last-24.md / digest.md / constants.md (regenerated); digest.md is a bounded projection of the current memory tree, not history — search instead.
<!-- yard:tools:end -->
```

**Checkpoint:** open a fresh session in the room; the agent sees the injected context, empty files produced no headers, and one retrieval command actually runs from the agent's working directory.

## Acceptance (you check, then report)

Run this list **yourself** — do not walk the user through a ceremony. Skip items for features left off; then give the user a short report of what passed and what's enabled:

- [ ] `--ingest-only` runs clean; message/turn counts grow when they chat
- [ ] (if a custom archive source was imported) the normalize/clean script was dry-run on a sample, rerunning ingest is idempotent, and spot checks show stable native IDs, canonical `source_uuid` / `session_id`, resolved room policy, and source order
- [ ] (if cards on) yesterday's cards exist; `cards-last-24.md` regenerates via `--rebuild-cards`
- [ ] (if last24 summary on) `summary-last-24.md` passes the 700-character submit/recheck gate and is visible in a fresh session
- [ ] Scheduled jobs are loaded; first nightly log ends with `nightly done`
- [ ] Fresh session shows injected memory and a working retrieval command
- [ ] The three transparency answers (cost / rhythm / data destination) were said out loud — if not, say them in the report
- [ ] Backup ran once: a `fragments-*.db.gz` exists in `settings.backup.dir`

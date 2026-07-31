# Stage 5 — Scheduling

Four jobs, at the default times you already reported in the interview (04:00 nightly; backup after it; ingest realtime or periodic):

| Job | Command | Cadence |
|---|---|---|
| Ingest | `bin/watch.sh --mode ingest` (realtime, needs fswatch) **or** cron `--ingest-only` + `--rebuild-cards` | realtime / every 10–15 min |
| Daytime cards | `bin/watch-cards.sh` (polls the source-independent DB turns watermark; cost switch is `watcher.auto_cards`) | realtime, 5-second DB poll |
| Nightly | `bin/nightly.sh` (index + cards + last24 summary + curator) | once, small hours (default 04:00) |
| Backup | `bin/backup-db.sh` | daily |

Implement platform-native:

- **macOS:** `bin/make-launchd.sh` generates separate ingest, DB card watcher, nightly, and backup plists and prints the `launchctl bootstrap` commands (it never loads anything itself — run those commands after reading its output). Defaults match the table; override with `--nightly HH:MM` / `--backup HH:MM`, and use `--ingest-interval <minutes>` instead of the fswatch watcher on machines without fswatch. `--no-ingest` does not disable the DB card watcher because another adapter may still write turns; use `--no-card-watch` explicitly if needed. launchd jobs don't inherit the user's PATH — if codex lives somewhere exotic, set `CLAUDE_MEMORY_CODEX_BIN` in `.env`.
- **Linux:** cron or systemd timers — this is exactly the part you adapt by hand. Note the BSD `stat -f%m` limitation in SKILL.md before using `watch.sh`/`nightly.sh` as-is.

**Checkpoint:** `launchctl list | grep <label>` (or `crontab -l` / `systemctl --user list-timers`) shows the jobs; after the next scheduled run, `data/nightly.log` ends with `nightly done`.

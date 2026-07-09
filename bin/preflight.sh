#!/usr/bin/env bash
# Preflight checks for a new Neroli install. Read-only: changes nothing.
# Exit 0 = all required checks pass; `note` lines are informational, not failures.
set -uo pipefail

PIPELINE="$(cd "$(dirname "$0")/.." && pwd)"
fail=0

ok()   { printf 'ok    %s\n' "$1"; }
bad()  { printf 'FAIL  %s\n' "$1"; fail=1; }
note() { printf 'note  %s\n' "$1"; }

# --- required ---

if command -v python3 >/dev/null; then
  pyv="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    ok "python3 $pyv (need >= 3.10)"
  else
    bad "python3 $pyv — need >= 3.10"
  fi
else
  bad "python3 not found"
fi

if command -v sqlite3 >/dev/null; then
  ok "sqlite3 $(sqlite3 --version | cut -d' ' -f1)"
else
  bad "sqlite3 not found"
fi

# 引擎会把 vendor/ 挂进 sys.path，这里按同一口径检查（pip 装的和 vendored 的都算数）
if python3 -c "import sys; sys.path.insert(0, '$PIPELINE/vendor'); import jieba, numpy, igraph, leidenalg" 2>/dev/null; then
  ok "python deps (jieba numpy python-igraph leidenalg)"
else
  bad "python deps missing — run: pip install -r $PIPELINE/requirements.txt"
fi

# --- optional (reported, never fatal) ---

if command -v fswatch >/dev/null; then
  ok "fswatch (realtime watcher available)"
else
  note "fswatch not found — optional; realtime ingest needs it (macOS: brew install fswatch), or use periodic ingest in the scheduling stage"
fi

if command -v codex >/dev/null; then
  ok "codex on PATH (the tested default for model_access.provider=cli)"
else
  note "codex not on PATH — fine with another CLI (model_access.cli_cmd) or provider=api; the night curator needs an agentic CLI either way"
fi

exit "$fail"

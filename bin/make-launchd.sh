#!/usr/bin/env bash
# Generate launchd plists for the Neroli jobs (macOS). Writes the files and
# prints the launchctl bootstrap commands — it does NOT load anything itself,
# so you can read the plists before committing to them.
#
# Usage: bin/make-launchd.sh [options]
#   --dir DIR              where to write plists (default ~/Library/LaunchAgents)
#   --prefix LABEL         label prefix (default com.neroli)
#   --nightly HH:MM        nightly job time (default 04:00)
#   --backup HH:MM         backup job time (default 04:40, after nightly)
#   --ingest-interval MIN  periodic ingest every MIN minutes instead of the
#                          fswatch realtime watcher (for machines without fswatch)
#   --no-ingest            skip the ingest job entirely
set -euo pipefail

PIPELINE="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$HOME/Library/LaunchAgents"
PREFIX="com.neroli"
NIGHTLY="04:00"
BACKUP="04:40"
INGEST_MODE="fswatch"   # fswatch | interval | none
INTERVAL_MIN=15

while [ $# -gt 0 ]; do
  case "$1" in
    --dir)             DIR="$2"; shift 2 ;;
    --prefix)          PREFIX="$2"; shift 2 ;;
    --nightly)         NIGHTLY="$2"; shift 2 ;;
    --backup)          BACKUP="$2"; shift 2 ;;
    --ingest-interval) INGEST_MODE="interval"; INTERVAL_MIN="$2"; shift 2 ;;
    --no-ingest)       INGEST_MODE="none"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# 10# 强制十进制，否则 04 这种带前导零的会被 bash 当八进制
h_of() { v="${1%%:*}"; echo "$((10#$v))"; }
m_of() { v="${1##*:}"; echo "$((10#$v))"; }

case "$NIGHTLY" in [0-9][0-9]:[0-9][0-9]|[0-9]:[0-9][0-9]) ;; *) echo "bad --nightly '$NIGHTLY' (want HH:MM)" >&2; exit 2 ;; esac
case "$BACKUP"  in [0-9][0-9]:[0-9][0-9]|[0-9]:[0-9][0-9]) ;; *) echo "bad --backup '$BACKUP' (want HH:MM)" >&2; exit 2 ;; esac

mkdir -p "$DIR" "$PIPELINE/data"
written=""

plist_head() { # label
  cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$1</string>
  <key>StandardOutPath</key><string>$PIPELINE/data/launchd-${1##*.}.log</string>
  <key>StandardErrorPath</key><string>$PIPELINE/data/launchd-${1##*.}.log</string>
EOF
}

emit_calendar() { # label command time
  local label="$1" cmd="$2" t="$3" f="$DIR/$1.plist"
  { plist_head "$label"
    cat <<EOF
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>-c</string><string>$cmd</string></array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$(h_of "$t")</integer><key>Minute</key><integer>$(m_of "$t")</integer></dict>
</dict>
</plist>
EOF
  } > "$f"
  written="$written$f"$'\n'
}

case "$INGEST_MODE" in
  fswatch)
    command -v fswatch >/dev/null || echo "warning: fswatch not on PATH — the watcher job will crash-loop until it is (or rerun with --ingest-interval N)" >&2
    f="$DIR/$PREFIX.watch.plist"
    { plist_head "$PREFIX.watch"
      cat <<EOF
  <key>ProgramArguments</key>
  <array><string>$PIPELINE/bin/watch.sh</string><string>--mode</string><string>ingest</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
EOF
    } > "$f"
    written="$written$f"$'\n'
    ;;
  interval)
    f="$DIR/$PREFIX.ingest.plist"
    { plist_head "$PREFIX.ingest"
      cat <<EOF
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>-c</string><string>cd '$PIPELINE' &amp;&amp; python3 bin/cli.py --ingest-only --max-messages 0 &amp;&amp; python3 bin/cli.py --rebuild-cards</string></array>
  <key>StartInterval</key><integer>$((INTERVAL_MIN * 60))</integer>
</dict>
</plist>
EOF
    } > "$f"
    written="$written$f"$'\n'
    ;;
  none) ;;
esac

emit_calendar "$PREFIX.nightly" "'$PIPELINE/bin/nightly.sh'" "$NIGHTLY"
emit_calendar "$PREFIX.backup"  "'$PIPELINE/bin/backup-db.sh'" "$BACKUP"

echo "wrote:"
printf '%s' "$written" | sed 's/^/  /'
if command -v plutil >/dev/null; then
  printf '%s' "$written" | while IFS= read -r f; do [ -n "$f" ] && plutil -lint "$f"; done
fi
echo
echo "load them with (bootout first if reloading an existing label):"
printf '%s' "$written" | while IFS= read -r f; do
  [ -n "$f" ] && echo "  launchctl bootstrap gui/\$(id -u) '$f'"
done
echo "check: launchctl list | grep ${PREFIX}"
echo "launchd does not inherit your PATH — if codex lives somewhere exotic, set CLAUDE_MEMORY_CODEX_BIN in .env"

#!/usr/bin/env bash
set -uo pipefail

# 白天 Card Gen watcher：轮询 SQLite turns 变更水位，来源无关地唤醒 --auto-cards。
# ingest / adapter 只负责写 messages/turns；nightly 仍是独立的冷 session 补漏流程。
#
# 用法：
#   watch-cards.sh          # launchd KeepAlive 常驻
#   watch-cards.sh --once   # 只检查一次（运维/测试）

PIPELINE="$(cd "$(dirname "$0")/.." && pwd)"
CLI="$PIPELINE/bin/cli.py"
DB="${CARD_WATCH_DB:-$PIPELINE/data/fragments.db}"
SETTINGS="${CARD_WATCH_SETTINGS:-$PIPELINE/config/settings.json}"
STATE="${CARD_WATCH_STATE:-$PIPELINE/data/card-watch-watermark}"
LOCKDIR="$PIPELINE/data/ingest.lock.d"
LOCK_LABEL="card-watch"
source "$PIPELINE/bin/lock-lib.sh"

ONCE=0
if [[ "${1:-}" == "--once" ]]; then
  ONCE=1
elif [[ $# -gt 0 ]]; then
  echo "usage: watch-cards.sh [--once]" >&2
  exit 2
fi

read_revision() {
  [[ -f "$DB" ]] || return 1
  sqlite3 "$DB" "SELECT revision FROM change_watermarks WHERE name='turns';" 2>/dev/null
}

read_setting() {
  python3 - "$SETTINGS" "$1" "$2" <<'PY'
import json
import sys

path, key, default = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as f:
        watcher = json.load(f).get("watcher", {})
    value = watcher.get(key, default)
except (OSError, ValueError, TypeError):
    value = default
if isinstance(value, bool):
    print("1" if value else "0")
else:
    print(value)
PY
}

write_state() {
  mkdir -p "$(dirname "$STATE")"
  local tmp="${STATE}.tmp.$$"
  printf '%s\n' "$1" > "$tmp"
  mv "$tmp" "$STATE"
}

poll_once() {
  local revision previous enabled
  revision="$(read_revision)" || {
    echo "[card-watch] $(date '+%F %T') DB/turns 水位不可读（是否尚未迁移 schema v10？）: $DB" >&2
    return 1
  }
  [[ "$revision" =~ ^[0-9]+$ ]] || {
    echo "[card-watch] 非法 turns revision: ${revision:-<empty>}" >&2
    return 1
  }

  previous="$(cat "$STATE" 2>/dev/null || true)"
  if [[ ! "$previous" =~ ^[0-9]+$ ]] || (( previous > revision )); then
    write_state "$revision"
    echo "[card-watch] $(date '+%F %T') 建立 turns 水位基线: $revision"
    return 0
  fi
  (( revision == previous )) && return 0

  # 先确认消费到哪个 DB revision；Card Gen 运行期间若又有 turns 写入，下一轮会再次看到。
  write_state "$revision"
  enabled="$(read_setting auto_cards false)"
  if [[ "$enabled" != "1" ]]; then
    echo "[card-watch] $(date '+%F %T') turns ${previous}->${revision}；auto_cards=false，跳过"
    return 0
  fi

  echo "[card-watch] $(date '+%F %T') turns ${previous}->${revision}；检查白天出卡阈值"
  if with_lock python3 "$CLI" --db "$DB" --auto-cards 2>&1 | sed 's/^/  [auto] /'; then
    return 0
  fi
  echo "[card-watch] 本次 auto-cards 失败；等待下一次 turns 变化再触发" >&2
  return 1
}

while :; do
  poll_once || true
  (( ONCE )) && exit 0
  interval="$(read_setting card_poll_seconds 5)"
  [[ "$interval" =~ ^[0-9]+$ ]] && (( interval > 0 )) || interval=5
  sleep "$interval"
done

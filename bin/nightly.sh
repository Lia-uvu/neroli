#!/usr/bin/env bash
# 夜间维护：默认只重聚类 → 刷 context-last-24。
# v4 Option A 保留旧卡层时，不自动补漏出卡，避免大额模型重跑。
set -euo pipefail

PIPELINE="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$PIPELINE/src"
DB="$PIPELINE/data/fragments.db"
LOG="$PIPELINE/data/nightly.log"
SETTINGS="$PIPELINE/config/settings.json"
LOCKDIR="$PIPELINE/data/ingest.lock.d"

exec >> "$LOG" 2>&1
echo "==== $(date '+%Y-%m-%d %H:%M:%S') nightly start ===="

cd "$SRC"

with_lock() {
  local tries=0
  until mkdir "$LOCKDIR" 2>/dev/null; do
    local age
    age=$(( $(date +%s) - $(stat -f%m "$LOCKDIR" 2>/dev/null || date +%s) ))
    (( age > 300 )) && rmdir "$LOCKDIR" 2>/dev/null && continue
    (( tries++ >= 600 )) && { echo "[nightly] lock 等待超时，跳过本步" >&2; return 1; }
    sleep 0.1
  done
  set +e
  "$@"
  local rc=$?
  set -e
  rmdir "$LOCKDIR" 2>/dev/null
  return $rc
}

GENERATE_MISSING_CARDS=$(python3 -c "import json; print('1' if json.load(open('$SETTINGS')).get('nightly',{}).get('generate_missing_cards', False) else '')")

# 1. 补漏：给白天没触发阈值的冷 session 出卡。
#    调用方式按 settings.model_access（cli/api/ollama），模型/effort 按 settings.card_gen，
#    全部由 CLI 自建；房间按来源自动派生。
if [[ -n "$GENERATE_MISSING_CARDS" ]]; then
  echo "[1/4] sweep cold sessions"
  with_lock python3 cli.py --process-existing --skip-processed \
    || echo "  (some sessions may have failed, continuing)"
else
  echo "[1/4] sweep cold sessions skipped (settings.nightly.generate_missing_cards=false)"
fi

# 2. 重建 v2 索引（Leiden hierarchy；settings.index 可切换/暂停）
echo "[2/4] rebuild index"
with_lock python3 cli.py --rebuild-index

# 3. 刷 context-last-24
echo "[3/4] rebuild context"
with_lock python3 cli.py --rebuild-context

# 4. 中期层（馆员）：快照 → 每房间工作台 + curator agent → 近况总结 digest + constants。
#    调用方式按 settings.model_access，模型按 settings.midlayer.model/reasoning_effort，CLI 自建。
#    midlayer.enabled=false 时整体跳过；只想攒快照不调模型，把这行换成 --curate-snapshot。
echo "[4/4] midlayer curate (snapshot + curator)"
with_lock python3 cli.py --curate \
  || echo "  (curate failed, continuing)"

echo "==== $(date '+%Y-%m-%d %H:%M:%S') nightly done ===="

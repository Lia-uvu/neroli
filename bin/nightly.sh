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
LOCK_LABEL="nightly"

exec >> "$LOG" 2>&1
echo "==== $(date '+%Y-%m-%d %H:%M:%S %z') nightly start ===="

# 报警：写日志之外，存在 bin/alert-local.sh（gitignored，部署方自备）就调它。
# 各步失败静默吞掉的教训：2026-07-11 curate 连崩，digest/constants 冻了三天才被人发现。
alert() {
  echo "[alert] $1"
  if [[ -x "$PIPELINE/bin/alert-local.sh" ]]; then
    "$PIPELINE/bin/alert-local.sh" "$1" || true
  fi
}
trap 'alert "nightly 在第 ${LINENO} 行意外退出，详见 data/nightly.log"' ERR

cd "$SRC"
source "$PIPELINE/bin/lock-lib.sh"

GENERATE_MISSING_CARDS=$(python3 -c "import json; print('1' if json.load(open('$SETTINGS')).get('nightly',{}).get('generate_missing_cards', False) else '')")

# 1. 补漏：给白天没触发阈值的冷 session 出卡。
#    调用方式按 settings.model_access（cli/api/ollama），模型/effort 按 settings.card_gen，
#    全部由 CLI 自建；房间按来源自动派生。
if [[ -n "$GENERATE_MISSING_CARDS" ]]; then
  echo "[1/5] sweep cold sessions"
  with_lock python3 cli.py --process-existing --skip-processed \
    || alert "补漏出卡失败（continuing）"
else
  echo "[1/5] sweep cold sessions skipped (settings.nightly.generate_missing_cards=false)"
fi

# 2. 重建 v2 索引（Leiden hierarchy；settings.index 可切换/暂停）
echo "[2/5] rebuild index"
with_lock python3 cli.py --rebuild-index

# 3. 预热 share 向量（--sem 跨房间匹配用；幂等，只补缺的，失败不挡后续）
echo "[3/5] backfill share vectors"
python3 "$PIPELINE/scripts/backfill_share_vecs.py" \
  || alert "share 向量预热失败，--sem 跨房间会漏新卡（continuing）"

# 4. 刷 context-last-24
echo "[4/5] rebuild context"
with_lock python3 cli.py --rebuild-context

# 5. 中期层（馆员）：快照 → 每房间工作台 + curator agent → 近况总结 digest + constants。
#    调用方式按 settings.model_access，模型按 settings.midlayer.model/reasoning_effort，CLI 自建。
#    midlayer.enabled=false 时整体跳过；只想攒快照不调模型，把这行换成 --curate-snapshot。
echo "[5/5] midlayer curate (snapshot + curator)"
with_lock python3 cli.py --curate \
  || alert "curate 失败，digest/constants 今晚没更新（continuing）"

echo "==== $(date '+%Y-%m-%d %H:%M:%S %z') nightly done ===="

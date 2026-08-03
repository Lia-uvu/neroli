#!/usr/bin/env zsh
set -euo pipefail

# 监听所有房间的 Claude Code session JSONL 变化（房间清单：config/rooms.json）。
#
# 用法：
#   watch.sh --mode ingest    # 只洗数据入 turns 表，零模型调用（launchd KeepAlive 常驻）
#                             # 出卡不在此触发：独立 watch-cards.sh 轮询 DB turns 水位；
#                             # 冷 session 仍靠 nightly 补漏
#
# 依赖：fswatch (brew install fswatch)

PIPELINE="$(cd "$(dirname "$0")/.." && pwd)"
CLI="$PIPELINE/bin/cli.py"
MODE=""
[[ "${1:-}" == "--mode" ]] && MODE="$2"
if [[ "$MODE" != "ingest" ]]; then
  echo "usage: watch.sh --mode ingest" >&2
  exit 2
fi

# 房间项目目录来自唯一事实源 rooms.json；不存在的先建出来（fswatch 不接受缺目录）
DIRS=()
while IFS= read -r dir; do
  mkdir -p "$dir"
  DIRS+=("$dir")
done < <(python3 -c "import json;[print(r['project_dir']) for r in json.load(open('$PIPELINE/config/rooms.json'))['rooms']]")

if ! command -v fswatch &>/dev/null; then
  echo "需要 fswatch: brew install fswatch" >&2
  exit 1
fi

DEBOUNCE=$(python3 -c "import json;print(json.load(open('$PIPELINE/config/settings.json')).get('watcher',{}).get('ingest_debounce_seconds',5))")
LOCKDIR="$PIPELINE/data/ingest.lock.d"
LOCK_LABEL="watch"
source "$PIPELINE/bin/lock-lib.sh"

echo "[watch:$MODE] 监听 ${DIRS[@]}"

last_ingest=0

fswatch -0 --include='\.jsonl$' --exclude='.*' "${DIRS[@]}" | while IFS= read -r -d $'\0' file; do
  now=$(date +%s)
  session_id="$(basename "$file" .jsonl)"

  # v4 聚合模型：任一文件变化都全量重读所有房间 JSONL（不再 per-file），跨文件去重 +
  # (session_id, source_uuid) DO UPDATE 重编号让整库一遍既廉价又幂等（零模型调用）。
  # debounce 用全局时钟（针对任意文件，不再 per-file），避免 fswatch 突发时反复全量。
  (( now - last_ingest < DEBOUNCE )) && continue
  last_ingest=$now
  echo "[watch:ingest] $(date '+%H:%M:%S') 检测到变化: $session_id（全量重读房间）"
  # 串行化：重叠的 fswatch 事件不会对同一 DB 并发跑两个 ingest。
  with_lock python3 "$CLI" --ingest-only --ingest-claude-tree --max-messages 0 2>&1 | sed 's/^/  /'
  with_lock python3 "$CLI" --rebuild-cards 2>&1 | sed 's/^/  /'
done

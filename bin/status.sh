#!/usr/bin/env bash
# 日常体检（只读，改变不了任何东西）。preflight.sh 管装机依赖，本脚本管"今天系统活得好吗"。
# 专抓静默失败：报警(alert-local)只能抓"报错了"，这里抓"该发生的没发生"。
# 时间判断一律走 epoch/UTC，不受机器时区与显示时区不一致的影响。
# 用法：bin/status.sh    exit 0=全绿（warn/note 不算红），1=有 FAIL
set -uo pipefail

PIPELINE="$(cd "$(dirname "$0")/.." && pwd)"
DB="$PIPELINE/data/fragments.db"
SETTINGS="$PIPELINE/config/settings.json"
fail=0

ok()   { printf 'ok    %s\n' "$1"; }
bad()  { printf 'FAIL  %s\n' "$1"; fail=1; }
warn() { printf 'warn  %s\n' "$1"; }
note() { printf 'note  %s\n' "$1"; }

now_epoch=$(date +%s)
age_h() { echo $(( (now_epoch - $1) / 3600 )); }
mtime_of() { stat -f %m "$1" 2>/dev/null || echo 0; }

# --- launchd 任务在不在 ---
jobs="$(launchctl list 2>/dev/null | grep -E 'recall|neroli' || true)"
watch_line="$(echo "$jobs" | grep -E 'watch' || true)"
if [[ -n "$watch_line" ]] && [[ "$(echo "$watch_line" | awk '{print $1}')" != "-" ]]; then
  ok "watcher 进程活着 (pid $(echo "$watch_line" | awk '{print $1}'))"
else
  bad "watcher 没有活进程 — launchctl list | grep -E 'recall|neroli' 查看；重启见 skills/ops/common.md"
fi
for kind in nightly backup; do
  if echo "$jobs" | grep -q "$kind"; then
    ok "launchd 任务已加载: $kind"
  else
    bad "launchd 任务缺失: $kind — plist 是否被 bootout 了？"
  fi
done

# --- nightly 上次真正跑完是什么时候 ---
NIGHTLY_LOG="$PIPELINE/data/nightly.log"
if [[ -f "$NIGHTLY_LOG" ]]; then
  h=$(age_h "$(mtime_of "$NIGHTLY_LOG")")
  if (( h <= 28 )); then
    ok "nightly 日志 ${h}h 前有写入"
  else
    bad "nightly 日志已 ${h}h 没动静（>28h）— 睡眠错过会在唤醒时补跑（StartCalendarInterval），所以先查：机器时区漂了？（incidents.md 07-15）job 被 bootout？脚本报错？"
  fi
  if tail -n 5 "$NIGHTLY_LOG" | grep -q "nightly done"; then
    ok "nightly 最后一轮正常收尾 (nightly done)"
  else
    warn "nightly 日志结尾不是 'nightly done' — 上一轮可能中途死掉，tail data/nightly.log"
  fi
  n_alerts=$(tail -n 40 "$NIGHTLY_LOG" | grep -c "\[alert\]" || true)
  (( n_alerts > 0 )) && warn "nightly 最近日志里有 ${n_alerts} 条 [alert]，去看 data/nightly.log"
else
  bad "找不到 $NIGHTLY_LOG"
fi

# --- DB 层：schema 版本 / 出卡 / curator ---
if [[ -f "$DB" ]]; then
  expected=$(grep -m1 "^SCHEMA_VERSION" "$PIPELINE/src/db.py" | tr -dc '0-9')
  actual=$(sqlite3 "$DB" "PRAGMA user_version;" 2>/dev/null)
  if [[ "$actual" == "$expected" ]]; then
    ok "schema v$actual (与 src/db.py 一致)"
  else
    bad "schema 版本不符: 库=v$actual 代码=v$expected — 有迁移没跑，见 migrations/"
  fi

  read -r turns24 cards24 <<< "$(sqlite3 -separator ' ' "$DB" "
    SELECT
      (SELECT COUNT(*) FROM messages WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-24 hours')),
      (SELECT COUNT(*) FROM cards    WHERE timestamp >= strftime('%Y-%m-%dT%H:%M:%SZ','now','-24 hours'));")"
  if (( cards24 > 0 )); then
    ok "近24h: ${turns24} 条消息 → ${cards24} 张卡"
  elif (( turns24 > 0 )); then
    bad "近24h 有 ${turns24} 条消息但 0 张卡 — 出卡静默失败？查 data/watch.log 和 codex（common.md 有剧本）"
  else
    note "近24h 没有新消息也没有新卡（安静的一天，或 ingest 断了——若今天明明聊过天，按出卡故障排查）"
  fi

  midlayer_on=$(python3 -c "import json;print(json.load(open('$SETTINGS')).get('midlayer',{}).get('enabled',False))")
  if [[ "$midlayer_on" == "True" ]]; then
    dig_age=$(sqlite3 "$DB" "SELECT CAST((julianday('now') - julianday(MAX(created_at)))*24 AS INT) FROM digests;" 2>/dev/null)
    if [[ -n "$dig_age" && "$dig_age" != "" ]] && (( dig_age <= 28 )); then
      ok "curator 上次产出 digest 是 ${dig_age}h 前 (最近快照夜: $(sqlite3 "$DB" "SELECT MAX(night) FROM tree_snapshots;"))"
    else
      bad "curator 已 ${dig_age:-?}h 没产出 digest（>28h）— 三晚事故重演？见 skills/ops/incidents.md"
    fi
  else
    note "midlayer.enabled=false，跳过 curator 检查"
  fi
else
  bad "找不到生产库 $DB"
fi

# --- share 向量覆盖率（--sem 跨房间匹配的原料，nightly [3/5] 预热）---
if [[ -f "$DB" ]]; then
  missing=$(python3 "$PIPELINE/scripts/backfill_share_vecs.py" --dry-run 2>/dev/null | grep -oE '缺 share 向量 [0-9]+' | tr -dc '0-9')
  if [[ -z "$missing" ]]; then
    warn "share 向量覆盖率查不出来 — scripts/backfill_share_vecs.py --dry-run 挂了？"
  elif (( missing == 0 )); then
    ok "share 向量全覆盖（--sem 跨房间不漏卡）"
  elif (( missing <= 40 )); then
    note "share 向量缺 ${missing} 张（今天的新卡，等今晚 nightly 补；持续增大才是问题）"
  else
    bad "share 向量缺 ${missing} 张 — nightly [3/5] 预热断了？--sem 跨房间在漏卡"
  fi
fi

# --- 备份新鲜度 ---
backup_dir=$(python3 -c "import json;print(json.load(open('$SETTINGS')).get('backup',{}).get('dir',''))")
if [[ -n "$backup_dir" ]]; then
  newest=$(ls -t "$backup_dir"/databases/fragments-*.db.gz 2>/dev/null | head -1)
  if [[ -n "$newest" ]]; then
    h=$(age_h "$(mtime_of "$newest")")
    if (( h <= 28 )); then ok "最新 DB 备份 ${h}h 前 ($(basename "$newest"))"
    else bad "最新 DB 备份已 ${h}h（>28h）— local.recall-backup 没跑？"; fi
  else
    bad "备份目录里没有任何 fragments-*.db.gz: $backup_dir/databases"
  fi
else
  warn "settings.backup.dir 未配置，跳过备份检查"
fi

# --- 模型 CLI 可达性（出卡/curator 的命脉）---
provider=$(python3 -c "import json;s=json.load(open('$SETTINGS')).get('model_access',{});print(s.get('provider','cli'))")
cli_cmd=$(python3 -c "import json;s=json.load(open('$SETTINGS')).get('model_access',{});print(s.get('cli_cmd',''))")
if [[ "$provider" == "cli" && -z "$cli_cmd" ]]; then
  codex_path=$(python3 -c "
import sys; sys.path.insert(0, '$PIPELINE/src')
try:
    from model import _codex_bin; print(_codex_bin())
except Exception as e:
    print('')" 2>/dev/null)
  if [[ -n "$codex_path" && -x "$codex_path" || "$codex_path" == "codex" ]]; then
    ok "codex 可达: $codex_path"
  else
    bad "codex 找不到 — App 更新常改内置路径；出卡/curator 会静默失败（common.md '运行时依赖'）"
  fi
else
  note "model_access: provider=$provider${cli_cmd:+, cli_cmd=$cli_cmd}（未做可达性探测）"
fi

# --- 信息行 ---
note "watch.log 最后写入 $(age_h "$(mtime_of "$PIPELINE/data/watch.log")")h 前（安静≠故障，仅供参考）"

exit "$fail"

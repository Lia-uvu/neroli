#!/usr/bin/env bash
# 每日记忆备份：SQLite 在线 .backup（写入中也安全）+ gzip + 滚动保留。
# kept 是各房间的常驻记忆，一并快照。目的地/保留份数读 settings.backup，房间读 rooms.json。
set -euo pipefail

PIPELINE="$(cd "$(dirname "$0")/.." && pwd)"
SETTINGS="$PIPELINE/config/settings.json"
ROOMS="$PIPELINE/config/rooms.json"

DEST=$(python3 -c "import json;print(json.load(open('$SETTINGS')).get('backup',{}).get('dir',''))")
KEEP=$(python3 -c "import json;print(json.load(open('$SETTINGS')).get('backup',{}).get('keep',14))")
if [[ -z "$DEST" ]]; then
  echo "settings.backup.dir 未配置，跳过备份" >&2
  exit 1
fi
DB_DEST="$DEST/databases"
KEPT_DEST="$DEST/kept-snapshots"
STAMP="$(date +%Y%m%d-%H%M)"

mkdir -p "$DB_DEST" "$KEPT_DEST"
sqlite3 "$PIPELINE/data/fragments.db" ".backup '$DB_DEST/fragments-$STAMP.db'"
gzip "$DB_DEST/fragments-$STAMP.db"

# 各房间 kept.md（room_dir 来自 rooms.json）
while IFS=$'\t' read -r name dir; do
  cp "$dir/kept.md" "$KEPT_DEST/kept-$name-$STAMP.md" 2>/dev/null || true
done < <(python3 -c "
import json
for r in json.load(open('$ROOMS'))['rooms']:
    print(f\"{r['name']}\t{r['room_dir']}\")
")

# 滚动：各类只留最近 KEEP 份
ls -t "$DB_DEST"/fragments-*.db.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs rm -f 2>/dev/null || true
while IFS=$'\t' read -r name dir; do
  ls -t "$KEPT_DEST"/kept-"$name"-*.md 2>/dev/null | tail -n +$((KEEP + 1)) | xargs rm -f 2>/dev/null || true
done < <(python3 -c "
import json
for r in json.load(open('$ROOMS'))['rooms']:
    print(f\"{r['name']}\t{r['room_dir']}\")
")
echo "backup ok: fragments-$STAMP.db.gz"

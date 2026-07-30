#!/usr/bin/env bash
# mkdir 锁的共同实现。调用方先设置 LOCKDIR / LOCK_LABEL，再 source 本文件。
# owner 文件记录真正运行命令的 PID + 本次 token：活 PID 永不按年龄抢锁，释放时也只
# 删除自己的 token，避免长模型任务被 300 秒误判为死锁后出现并发写库。

with_lock() {
  local tries=0 token owner owner_pid child_pid rc age mtime wait_tries
  token="$$-${RANDOM:-0}-$(date +%s)"
  : "${LOCKDIR:?LOCKDIR must be set before sourcing lock-lib.sh}"
  : "${LOCK_LABEL:=lock}"

  until mkdir "$LOCKDIR" 2>/dev/null; do
    owner="$(cat "$LOCKDIR/owner" 2>/dev/null || true)"
    owner_pid="${owner%%:*}"
    if [[ "$owner_pid" =~ ^[0-9]+$ ]] && ! kill -0 "$owner_pid" 2>/dev/null; then
      # owner 已死才回收；先删 owner 再 rmdir，目录仍存在期间别人无法插队创建。
      rm -f "$LOCKDIR/owner"
      rmdir "$LOCKDIR" 2>/dev/null || true
      continue
    elif [[ ! "$owner_pid" =~ ^[0-9]+$ ]]; then
      mtime="$(stat -f%m "$LOCKDIR" 2>/dev/null || stat -c%Y "$LOCKDIR" 2>/dev/null || date +%s)"
      age=$(( $(date +%s) - mtime ))
      if (( age > 5 )); then
        rm -f "$LOCKDIR/owner"
        rmdir "$LOCKDIR" 2>/dev/null || true
        continue
      fi
    fi
    wait_tries="${LOCK_WAIT_TRIES:-600}"
    if (( wait_tries > 0 && tries++ >= wait_tries )); then
      mtime="$(stat -f%m "$LOCKDIR" 2>/dev/null || stat -c%Y "$LOCKDIR" 2>/dev/null || date +%s)"
      age=$(( $(date +%s) - mtime ))
      echo "[$LOCK_LABEL] lock 等待超时（owner=${owner:-unknown}, held=${age}s），跳过本次" >&2
      return 1
    fi
    sleep 0.1
  done

  # 创建成功后先用当前 shell 占位，消除 mkdir→启动 child 之间的无 owner 窗口。
  printf '%s:%s\n' "$$" "$token" > "$LOCKDIR/owner"
  "$@" &
  child_pid=$!
  printf '%s:%s\n' "$child_pid" "$token" > "$LOCKDIR/owner"
  if wait "$child_pid"; then rc=0; else rc=$?; fi

  owner="$(cat "$LOCKDIR/owner" 2>/dev/null || true)"
  if [[ "$owner" == *":$token" ]]; then
    rm -f "$LOCKDIR/owner"
    rmdir "$LOCKDIR" 2>/dev/null || true
  fi
  return "$rc"
}

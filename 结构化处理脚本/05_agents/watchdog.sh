#!/bin/bash
# 【⚠️ 已废弃（2026-11-05，路径 A）】restore_narrative.py 已废弃（细颗粒抽取并入
# run_batch.py 四叙述板块），本看门狗探活的 restore_narrative 进程不再作为主链路。
# 保留作历史参照；新批次不再需要本脚本。
#
# watchdog.sh — 进程存活看门狗 + 自动拉起（横切保障层常驻组件）
#
# 实战来源：2026-10-25 19:24 后台还原进程被 WorkBuddy 跨会话 context 回收强杀，
#           脚本无看门狗，静默停摆 4.75h 才被人工发现重启。本脚本补此缺口。
#
# 职责：
#   1) 进程探活：周期性 pgrep 批次进程，死亡即按 launch_restore.sh 自动拉起。
#   2) 假死判定：进程在但 done 文件长时间无更新 → 强杀后拉起（应对"活但卡死"）。
#   3) 与断点续跑正交：自动拉起解决"谁救"，断点续跑解决"救起后不重做"。
#
# 部署（守护化，脱离 WorkBuddy 会话，可抗宿主回收）：
#   setsid bash /Users/Admin/WorkBuddy/乳腺门诊/结构化处理脚本/05_agents/watchdog.sh \
#        >> /tmp/watchdog.log 2>&1 < /dev/null &
#
set -u

WORK_DIR="/Users/Admin/WorkBuddy/乳腺门诊"
LAUNCH="$WORK_DIR/结构化处理脚本/05_agents/launch_restore.sh"
PROC_MATCH="bin/python.*restore_narrative"
DONE_FILE="$WORK_DIR/中间产物/_standardize/restore_done.txt"
CHECK_INTERVAL=60      # 探活间隔（秒）
STALE_WARN=1800        # done 无更新超过此秒数 → 观察告警（可能处重试/退避窗口）
STALE_KILL=2400        # 超过此秒数且进程仍在 → 判定假死，杀后拉起（2400s=40min，放过正常180s重试窗口）
RELAUNCH_GUARD=120     # 两次拉起最小间隔（秒），防竞态重复启动
TOTAL=80718            # 总记录数；达到即判定批次完成，不再拉起
LOG=/tmp/watchdog.log
PY3=/Users/Admin/.workbuddy/binaries/python/versions/3.13.12/bin/python3

log(){ echo "$(date '+%Y-%m-%d %H:%M:%S') [watchdog] $*"; }

if [ ! -x "$LAUNCH" ]; then
  log "ERROR: 启动器不存在或不可执行: $LAUNCH"
  exit 1
fi

LAST_LAUNCH=0
log "看门狗启动: 探活间隔=${CHECK_INTERVAL}s, 进程匹配='${PROC_MATCH}', 假死阈值=${STALE_KILL}s"

maybe_relaunch(){
  local reason="$1"
  local now; now=$(date +%s)
  if [ $((now - LAST_LAUNCH)) -lt "$RELAUNCH_GUARD" ]; then
    log "SKIP: ${reason} 但 ${RELAUNCH_GUARD}s 内已拉起, 避免重复启动"
    return
  fi
  log "${reason} → 自动拉起(后台, 循环继续监控)"
  bash "$LAUNCH" &
  LAST_LAUNCH=$(date +%s)
}

while true; do
  sleep "$CHECK_INTERVAL"
  # 完成判定：done 去重数 >= 总数 → 批次已结束，空闲不再拉起（防空转/误重启）
  if [ -f "$DONE_FILE" ]; then
    # 用 python 统计去重数，避免 sort 在受限环境下间歇性失败导致完成判定失效。
    done_cnt=$("$PY3" -c "import sys
try:
    print(len({x.strip() for x in open(sys.argv[1]) if x.strip()}))
except Exception:
    print(-1)" "$DONE_FILE" 2>/dev/null)
    done_cnt=${done_cnt:-0}
    if [ "$done_cnt" -ge 0 ] && [ "$done_cnt" -ge "$TOTAL" ]; then
      log "COMPLETE: done ${done_cnt} >= ${TOTAL}, 批次已完成, 进入空闲(不再拉起)"
      sleep 300
      continue
    fi
  fi
  if pgrep -f "$PROC_MATCH" >/dev/null 2>&1; then
    # 进程在：检查是否假死（长时间无产出）
    if [ -f "$DONE_FILE" ]; then
      age=$(( $(date +%s) - $(stat -f %m "$DONE_FILE") ))
      if [ "$age" -gt "$STALE_KILL" ]; then
        log "STALE_KILL: 进程存活但 done ${age}s 无更新(> ${STALE_KILL})→判定假死, 强杀后拉起"
        pkill -f "$PROC_MATCH" || true
        sleep 5
        maybe_relaunch "RELAUNCH-after-stale"
      elif [ "$age" -gt "$STALE_WARN" ]; then
        log "WARN: 进程存活, done ${age}s 无更新(可能处重试/退避窗口, 仅观察)"
      else
        log "OK: 进程存活, done 近期更新(${age}s)"
      fi
    fi
  else
    maybe_relaunch "DEAD: 未检测到批次进程"
  fi
done

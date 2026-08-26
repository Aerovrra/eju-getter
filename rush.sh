#!/usr/bin/env bash
# 成绩公布日的一条龙：等站点可用 → 预热登录（学好 cookie/结构）→ 到点开抢。
#
# 用法：
#   nohup caffeinate -i ./rush.sh 23:45 > /dev/null 2>&1 &     # 23:45 开始盯，0:00 出分
#   tail -f out/rush.log                                        # 看进度
#   ./rush.sh --stop                                            # 停掉
#
# 第一个参数是开始盯的时间（HH:MM，默认 23:45）。刻意比公布时间早一点开始，
# 这样公布瞬间会话已经是热的，一轮只花 1 个请求。
set -uo pipefail
cd "$(dirname "$0")"

LOG=out/rush.log
PIDFILE=out/rush.pid
START_AT="${1:-23:45}"
INTERVAL="${INTERVAL:-12}"        # 正常轮询间隔（秒）
FAST="${FAST:-4}"                 # 拥堵/报错时的重试间隔
HOURS="${HOURS:-8}"               # 最长盯多久
PROBE_MAX="${PROBE_MAX:-60}"      # 等站点结束维护最多探测多少次（每次 120 秒）

mkdir -p out

if [ "$START_AT" = "--stop" ]; then
  if [ -f "$PIDFILE" ]; then
    pid=$(cat "$PIDFILE")
    pkill -TERM -P "$pid" 2>/dev/null
    kill -TERM "$pid" 2>/dev/null
    rm -f "$PIDFILE"
    echo "已停止 (pid $pid)"
  else
    echo "没有在跑的 rush"
  fi
  exit 0
fi

# 防止重复挂
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  echo "已经有一个 rush 在跑了 (pid $(cat "$PIDFILE"))，先 ./rush.sh --stop" >&2
  exit 1
fi
echo $$ > "$PIDFILE"
trap 'rm -f "$PIDFILE"' EXIT

{
  echo "================================================================"
  echo "rush 启动 $(date '+%F %T %Z')  开始盯的时间=$START_AT 间隔=${INTERVAL}s"

  # 1) 等站点可用（维护窗口 / 临时 503 都在这里等）
  for i in $(seq 1 "$PROBE_MAX"); do
    code=$(curl -s -o /dev/null -w '%{http_code}' -A 'Mozilla/5.0' \
           https://eju-online.jasso.go.jp/src/CMNLOGIN010.php || echo 000)
    echo "$(date '+%T') 站点探测 #$i -> HTTP $code"
    [ "$code" = "200" ] && break
    sleep 120
  done

  # 2) 预热：登录一次，把 cookie 和站点结构学好，顺便确认凭据没问题
  echo "$(date '+%T') 预热登录 ……"
  if ./eju.sh fetch; then
    echo "$(date '+%T') 预热成功"
  else
    echo "$(date '+%T') 预热失败（watch 会自己重新登录，继续）"
  fi

  # 3) 到点开抢
  echo "$(date '+%T') 交给 watch，开始盯 $START_AT"
  ./eju.sh watch --start-at "$START_AT" \
      --interval "$INTERVAL" --fast-interval "$FAST" --hours "$HOURS"
  echo "$(date '+%T') watch 结束，退出码 $?"
} >> "$LOG" 2>&1

#!/bin/bash
# doctor.sh — CellBridge 全链路一键体检。
#
# 设计原则：**绝不直接读写 AT 串口**。网关是 PTY 的唯一读者，任何第三方
# 去写 AT 都会把网关的响应抢走，反而制造故障（这个坑踩过）。所以本脚本
# 只做只读检查：看进程、看日志、看数据库、看健康接口。
#
# 用法：./doctor.sh
set -uo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"
RUN="$HOME/.cellbridge/run"
LOG="$RUN/logs/gateway.log"
DB="$HOME/.cellbridge/data/cellbridge.sqlite"
TOKEN_FILE="$HOME/.cellbridge/push_token"

ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
head_() { printf '\n\033[1m%s\033[0m\n' "$1"; }

echo "═══════════════════════════════════════════════"
echo " CellBridge 体检  $(date '+%Y-%m-%d %H:%M:%S')"
echo "═══════════════════════════════════════════════"

head_ "1. 进程"
GW=$(pgrep -f "cellbridge-gateway" | wc -l | tr -d ' ')
AB=$(pgrep -f "voice-audio-bridge" | wc -l | tr -d ' ')
PTY=$(pgrep -f "at_pty_bridge" | wc -l | tr -d ' ')
[ "$GW" -ge 1 ] && ok "网关 cellbridge-gateway × $GW" || bad "网关未运行 → ./start_cellbridge.sh"
[ "$AB" -ge 1 ] && ok "音频桥 voice-audio-bridge × $AB" || bad "音频桥未运行（通话会没声音）"
[ "$PTY" -ge 1 ] && ok "AT 串口桥 at_pty_bridge × $PTY" || bad "AT 桥未运行（短信/通话全废）"
if [ -f "$RUN/pty_path.txt" ]; then
  P=$(cat "$RUN/pty_path.txt")
  [ -c "$P" ] && ok "PTY $P 存在" || bad "PTY $P 不存在"
fi

head_ "2. 网关健康"
H=$(curl -s -m 5 --noproxy '*' http://127.0.0.1:8787/api/v1/health 2>&1)
case "$H" in
  *'"status":"ok"'*) ok "HTTP 接口正常  $H" ;;
  *) bad "HTTP 接口异常：$H" ;;
esac

head_ "3. AT 链路（从日志推断，不碰串口）"
if [ -f "$LOG" ]; then
  N=$(tail -400 "$LOG" | grep -ciE "context deadline exceeded|at exchange failed|AT channel not ready" || true)
  [ "$N" -eq 0 ] && ok "最近 400 行无 AT 超时/失败" || bad "最近 400 行有 $N 条 AT 超时/失败"
  tail -400 "$LOG" | grep -E "modem AT channel ready" | tail -1 | sed 's/^/  · /'
fi

head_ "4. SIP 客户端（YakPhone）注册状态"
if [ -f "$LOG" ]; then
  LAST=$(grep "sip register user=iphone" "$LOG" | grep "expires=300" | tail -1)
  if [ -n "$LAST" ]; then
    TS=$(printf '%s' "$LAST" | awk '{print $1" "$2}')
    EPOCH=$(date -j -f "%Y/%m/%d %H:%M:%S" "$TS" +%s 2>/dev/null || echo 0)
    AGE=$(( $(date +%s) - EPOCH ))
    CONTACT=$(printf '%s' "$LAST" | sed -n 's/.*contact="<sip:iphone@\([^"]*\)".*/\1/p')
    if [ "$AGE" -le 60 ]; then
      ok "已注册（${AGE}s 前，$CONTACT）→ 来电会直接下发 SIP INVITE"
    elif [ "$AGE" -le 300 ]; then
      warn "注册已 ${AGE}s 未刷新（$CONTACT）→ App 可能刚进后台，来电靠推送"
    else
      bad "最后注册在 ${AGE}s 前 → YakPhone 未在运行/未连上，来电只能靠 VoIP 推送唤醒"
    fi
  else
    UP=$(head -1 "$LOG" | awk '{print $1" "$2}')
    bad "本次启动（$UP）后未收到任何注册 → YakPhone 当前没连上"
    printf '  · 前台打开 YakPhone 应立刻出现 sip register；若一直不来，检查\n'
    printf '    服务器地址填的是否为下方第 8 节的局域网 IP、账号 iphone/cellbridge-idoer\n'
  fi
fi

head_ "5. 推送（CallKit 唤醒的唯一通道）"
if [ -f "$TOKEN_FILE" ] && [ -s "$TOKEN_FILE" ]; then
  ok "token 已配置（$(tr -d '[:space:]' < "$TOKEN_FILE" | wc -c | tr -d ' ') 字符）"
  printf '  → 立即验证：./test-push.sh\n'
  if grep -q 'push_token: ""' "$RUN/config.yaml" 2>/dev/null; then
    warn "但运行中的 config.yaml 里 push_token 为空 → 改完 token 后忘了重启"
  fi
else
  bad "token 未配置 → App 在后台/锁屏时来电不会振铃"
  printf '  → 修复：./set-push-token.sh '\''<YakPhone 设置里的 PushKit token>'\'' 然后 ./start_cellbridge.sh\n'
fi
if [ -f "$LOG" ]; then
  tail -400 "$LOG" | grep -E "yakpush (sent|rejected|failed)" | tail -2 | sed 's/^/  · /'
fi

head_ "6. 短信"
if [ -f "$DB" ]; then
  sqlite3 -header -column "$DB" \
    "select direction as 方向, status as 状态, count(*) as 条数 from messages group by 1,2 order by 1,2;" 2>&1 | sed 's/^/  /'
  printf '  · 最近 3 条：\n'
  sqlite3 -column "$DB" \
    "select direction, peer, substr(body,1,20), encoding, status, datetime(created_at,'unixepoch','localtime') from messages order by created_at desc limit 3;" 2>&1 | sed 's/^/    /'
  STUCK=$(sqlite3 "$DB" "select count(*) from messages where status='queued';" 2>/dev/null || echo 0)
  [ "$STUCK" -eq 0 ] && ok "无卡在 queued 的短信" || bad "$STUCK 条仍卡在 queued"
fi

head_ "7. 最近通话"
if [ -f "$DB" ]; then
  sqlite3 -column "$DB" \
    "select direction, peer, state, coalesce(end_reason,'-'), datetime(started_at,'unixepoch','localtime') from calls order by started_at desc limit 4;" 2>&1 | sed 's/^/  /'
fi
if [ -f "$LOG" ]; then
  printf '  · 最近来电关键事件：\n'
  tail -400 "$LOG" | grep -E "sip inbound (invite sent|provisional|ringing|call declined|connected|answer failed|ring timeout)|sip bye received" | tail -6 | sed 's/^/    /'
fi

head_ "8. YakPhone 该填的服务器地址"
for IF in en0 en1; do
  IP=$(ipconfig getifaddr $IF 2>/dev/null || true)
  [ -n "$IP" ] && printf '  %s: %s:5060   账号 iphone / cellbridge-idoer\n' "$IF" "$IP"
done
TL=$(/Applications/Tailscale.app/Contents/MacOS/Tailscale ip -4 2>/dev/null || tailscale ip -4 2>/dev/null || true)
[ -n "$TL" ] && printf '  tailscale: %s:5060\n' "$(printf '%s' "$TL" | head -1)"

printf '\n\033[1m结论速查\033[0m\n'
printf '  · 短信不通      → 看第 1、3 节\n'
printf '  · 来电不响      → 看第 4、5 节（后台/锁屏必须靠推送）\n'
printf '  · 接起来没声音  → 看第 2 节与 logs/audio-bridge.log\n'
printf '  · 排查完把本节输出整段发我\n\n'

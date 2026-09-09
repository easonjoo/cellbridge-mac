#!/bin/bash
# start_cellbridge.sh — 在 Mac 上启动 CellBridge SIP 网关（DJiPhone Kit 的 iPhone 通话伴侣）
#
# 组件：
#   1. at_pty_bridge.py     USB AT ↔ PTY 串口桥（互斥：会先退出 DJiPhone Kit App）
#   2. voice-audio-bridge   蜂窝 UAC 音频 ↔ FIFO（CellBridge raw-pcm 后端）
#   3. cellbridge-gateway   SIP 服务器 + 短信引擎（iPhone 经 SIP/Tailscale 接入）
#
# 前提：模块侧语音运行时已部署（语音路由 ready）。
#   若刚重启过模块，请先打开 DJiPhone Kit.app 让它自动部署一次，再跑本脚本。
#
# 用法：./start_cellbridge.sh          启动（Ctrl+C 停止全部组件）
#       ./start_cellbridge.sh stop     停止全部组件

set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
RUN="$HOME/.cellbridge/run"
DATA="$HOME/.cellbridge/data"
LOG="$RUN/logs"
PY="${PYTHON:-$HOME/.workbuddy/binaries/python/envs/default/bin/python3}"
APP_PY="$DIR/at_pty_bridge.py"
BRIDGE="$DIR/voice-audio-bridge"
GATEWAY="$DIR/cellbridge-gateway"
SIP_USER="${SIP_USER:-iphone}"
SIP_PASS="${SIP_PASS:-cellbridge-$(id -un)}"

mkdir -p "$RUN" "$DATA" "$LOG"

if [ "${1:-}" = "stop" ]; then
  for pat in "cellbridge-gateway" "voice-audio-bridge" "at_pty_bridge.py"; do
    pkill -f "$pat" 2>/dev/null && echo "已停止 $pat"
  done
  exit 0
fi

# --- 前置检查 ---
for f in "$APP_PY" "$BRIDGE" "$GATEWAY"; do
  if [ ! -f "$f" ]; then
    echo "缺少组件: $f"; echo "请先运行构建（见 README-mac.md）"; exit 1
  fi
done

# App 占用 USB AT 接口，必须先退出
if pgrep -f "DJiPhone Kit.app" >/dev/null 2>&1; then
  echo "DJiPhone Kit App 正在运行（占用 USB AT 接口），先退出它..."
  pkill -f "DJiPhone Kit.app" 2>/dev/null
  sleep 2
fi

# 清理旧实例
"$0" stop >/dev/null 2>&1
sleep 1

# --- FIFO ---
RX_FIFO="$RUN/cellular-rx.fifo"
TX_FIFO="$RUN/cellular-tx.fifo"
rm -f "$RX_FIFO" "$TX_FIFO"
mkfifo "$RX_FIFO" "$TX_FIFO"

# --- 组件 1：AT PTY 桥 ---
echo "[1/3] 启动 AT PTY 桥..."
"$PY" "$APP_PY" > "$RUN/pty_path.txt" 2> "$LOG/at-pty.log" &
sleep 4
TTY_PATH=$(head -1 "$RUN/pty_path.txt" 2>/dev/null)
if [ -z "$TTY_PATH" ]; then
  echo "AT PTY 桥未输出串口路径，查看 $LOG/at-pty.log"; exit 1
fi
echo "    模块串口: $TTY_PATH"

# --- 组件 2：音频桥（FIFO 模式）---
echo "[2/3] 启动音频桥（FIFO 模式）..."
"$BRIDGE" --fifo-rx "$RX_FIFO" --fifo-tx "$TX_FIFO" --verbose \
  > /dev/null 2> "$LOG/audio-bridge.log" &

# --- 组件 3：网关 ---
echo "[3/3] 启动 CellBridge 网关..."
cat > "$RUN/config.yaml" << EOF
# 由 start_cellbridge.sh 生成
network:
  mode: tailnet
  transport: tailnet
server:
  listen: 127.0.0.1:8787
data:
  dir: $DATA
modem:
  adapter: at
  tty: $TTY_PATH
  baud: 9600
voice:
  enabled: true
  backend: raw-pcm
  rx_path: $RX_FIFO
  tx_path: $TX_FIFO
  sample_rate: 8000
sip:
  enabled: true
  listen: 0.0.0.0:5060
  realm: cellbridge
  users:
    - username: $SIP_USER
      password: $SIP_PASS
recording:
  enabled: false
EOF

"$GATEWAY" -config "$RUN/config.yaml" > "$LOG/gateway.log" 2>&1 &
GWPID=$!

sleep 6
if ! kill -0 $GWPID 2>/dev/null; then
  echo "网关启动失败，日志："; tail -20 "$LOG/gateway.log"; exit 1
fi

echo ""
echo "═══════════════════════════════════════════════"
echo " CellBridge 网关已启动"
echo "   SIP:     0.0.0.0:5060（局域网/Tailscale 可达）"
echo "   账号:    $SIP_USER / $SIP_PASS"
echo "   控制台:  http://127.0.0.1:8787"
echo "   日志:    $LOG/"
echo "═══════════════════════════════════════════════"
echo "iPhone 端（YakPhone 等 SIP 客户端）："
echo "   服务器 = Mac 的局域网 IP 或 Tailscale 地址:5060"
echo "   用户名/密码如上。停止：./start_cellbridge.sh stop"
echo ""
trap 'echo "停止全部组件..."; "$0" stop' INT TERM
wait $GWPID 2>/dev/null
"$0" stop

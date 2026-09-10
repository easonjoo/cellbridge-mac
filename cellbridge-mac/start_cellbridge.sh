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
PY="${PYTHON:-}"
if [ -z "$PY" ] || [ ! -x "$PY" ] || ! "$PY" -c "import usb.core" >/dev/null 2>&1; then
  # 优先挑已经装了 pyusb 的解释器；都不行就取第一个可用的（AT 桥启动时会报错）
  PY=""
  for cand in \
    "$HOME/.workbuddy/binaries/python/envs/default/bin/python3" \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3 \
    "$(command -v python3 2>/dev/null)" \
    /usr/bin/python3
  do
    [ -n "$cand" ] && [ -x "$cand" ] || continue
    if "$cand" -c "import usb.core" >/dev/null 2>&1; then PY="$cand"; break; fi
    [ -z "$PY" ] && PY="$cand"
  done
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
  echo "找不到 python3，请安装（brew install python3）或设置 PYTHON=/path/to/python3"; exit 1
fi
"$PY" -c "import usb.core" >/dev/null 2>&1 \
  || echo "警告：$PY 缺少 pyusb，AT 桥会失败。安装：$PY -m pip install --user pyusb"
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

# --- 语音路由（01.001.02.004 固件，会话类型含 CSVoice/VoLTE/VoiceMMode）---
# 模块重启后 mixer 复位，每次启动时重新写入；mini_tinymix 由 module-tools 交叉编译
echo "[0/3] 写入语音路由（AFE_PCM ↔ 全部语音会话类型）..."
if command -v adb >/dev/null 2>&1 || [ -x "$HOME/Applications/platform-tools/adb" ]; then
  export PATH="$HOME/Applications/platform-tools:$PATH"
  adb shell '
    [ -x /data/mini_tinymix ] || exit 0
    T=/data/mini_tinymix
    $T set "AFE_PCM_RX_Voice Mixer CSVoice" 1
    $T set "AFE_PCM_RX_Voice Mixer VoLTE" 1
    $T set "AFE_PCM_RX_Voice Mixer VoiceMMode1" 1
    $T set "AFE_PCM_RX_Voice Mixer VoiceMMode2" 1
    $T set "Voice_Tx Mixer AFE_PCM_TX_Voice" 1
    $T set "VoLTE_Tx Mixer AFE_PCM_TX_VoLTE" 1
    $T set "VoiceMMode1_Tx Mixer AFE_PCM_TX_MMode1" 1
    $T set "VoiceMMode2_Tx Mixer AFE_PCM_TX_MMode2" 1
  ' 2>/dev/null && echo "    语音路由已写入" || echo "    警告：语音路由写入失败（模块未连接？）"
  # mavo-pcm-bridge：DSP VoLTE ↔ UAC/USB 的用户态桥（缺它则通话全零静音）。
  # 注意：必须用 pidof 精确匹配（pgrep -f 会自匹配 adb shell 命令行造成假阳性）；
  # 二进制用 nohup 启动可在 adb shell 退出后存活。
  adb shell 'pidof mavo-pcm-bridge >/dev/null || { [ -x /data/mavo-pcm-bridge ] && nohup /data/mavo-pcm-bridge --verbose --voice-route-session > /data/mavo-bridge.log 2>&1 & sleep 2; }' 2>/dev/null
  adb shell 'pidof mavo-pcm-bridge >/dev/null && echo "    mavo-pcm-bridge 运行中" || echo "    警告：mavo-pcm-bridge 未运行"' 2>/dev/null
  # 部署路由自愈脚本到模块（幂等）
  adb shell '[ -x /data/voice-route-watchdog.sh ] || { cat > /data/voice-route-watchdog.sh <<EOF
#!/system/bin/sh
T=/data/mini_tinymix
while true; do
  \$T set "AFE_PCM_RX_Voice Mixer CSVoice" 1
  \$T set "AFE_PCM_RX_Voice Mixer VoLTE" 1
  \$T set "AFE_PCM_RX_Voice Mixer VoiceMMode1" 1
  \$T set "AFE_PCM_RX_Voice Mixer VoiceMMode2" 1
  \$T set "Voice_Tx Mixer AFE_PCM_TX_Voice" 1
  \$T set "VoLTE_Tx Mixer AFE_PCM_TX_VoLTE" 1
  \$T set "VoiceMMode1_Tx Mixer AFE_PCM_TX_MMode1" 1
  \$T set "VoiceMMode2_Tx Mixer AFE_PCM_TX_MMode2" 1
  sleep 3
done
EOF
chmod +x /data/voice-route-watchdog.sh; }' 2>/dev/null
  # 语音路由 watchdog：通话挂断时 DSP 会把 VoLTE 路由复位，需每 3 秒补写。
  # 必须从 Mac 侧用持久 adb 会话托管（模块侧 nohup/setsid 启动的 shell 脚本
  # 会随 adb shell 退出被杀，二进制则可存活）。
  adb shell sh /data/voice-route-watchdog.sh > /dev/null 2>&1 &
  sleep 1
  if kill -0 $! 2>/dev/null; then echo "    语音路由 watchdog 运行中（Mac 托管）"; else echo "    警告：watchdog 启动失败"; fi
else
  echo "    警告：找不到 adb，跳过 CS 路由写入"
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
# 真实短信（不设则默认 dry-run）
export CELLBRIDGE_SMS_DRY_RUN="${CELLBRIDGE_SMS_DRY_RUN:-false}"

# --- PushKit token（来电 CallKit 振铃 / 短信通知唤醒的必要条件）---
# 获取方式：YakPhone → 设置 → 推送/Push 页 → 复制 Push Token（形如 AAA...==）。
# 把 token 粘进 $HOME/.cellbridge/push_token 即可，无需手改 config.yaml。
# 不填的后果：App 在前台时 SIP INVITE 仍能振铃，但 App 挂起/后台时
# 来电不会有任何反应（CallKit 靠 VoIP 推送唤醒）。
PUSH_TOKEN="${YAK_PUSH_TOKEN:-}"
PUSH_TOKEN_FILE="$HOME/.cellbridge/push_token"
if [ -z "$PUSH_TOKEN" ] && [ -f "$PUSH_TOKEN_FILE" ]; then
  PUSH_TOKEN="$(tr -d '[:space:]' < "$PUSH_TOKEN_FILE")"
fi

echo "[3/3] 启动 CellBridge 网关... (SMS_DRY_RUN=$CELLBRIDGE_SMS_DRY_RUN)"
if [ -n "$PUSH_TOKEN" ]; then
  echo "    PushKit token: 已配置（${#PUSH_TOKEN} 字符）→ 来电可唤醒 CallKit"
else
  echo "    PushKit token: 未配置 —— 后台来电不会振铃（CallKit 需要 VoIP 推送）"
  echo "                   把 YakPhone 的 Push Token 写入 $PUSH_TOKEN_FILE 后重启本脚本"
fi
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
  push_token: "$PUSH_TOKEN"
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

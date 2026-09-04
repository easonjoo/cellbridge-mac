#!/bin/bash
# EG25-G SMS Web Server 启动脚本
# 启动后自动打开浏览器 http://localhost:8080
# 自动适配：libusb 路径（Apple Silicon / Intel）与 Python venv 路径

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT=8080

# --- 自动探测 libusb 路径 ---
if [ -d "/opt/homebrew/lib" ]; then
  export DYLD_LIBRARY_PATH=/opt/homebrew/lib
elif [ -d "/usr/local/lib" ]; then
  export DYLD_LIBRARY_PATH=/usr/local/lib
fi

# --- 自动探测 Python venv ---
if [ -x "$HOME/.workbuddy/binaries/python/envs/default/bin/python3" ]; then
  PYTHON="$HOME/.workbuddy/binaries/python/envs/default/bin/python3"
elif [ -x "/Users/zxd/.workbuddy/binaries/python/envs/default/bin/python3" ]; then
  PYTHON="/Users/zxd/.workbuddy/binaries/python/envs/default/bin/python3"
else
  PYTHON=python3
fi

echo "================================"
echo "  EG25-G 短信助手"
echo "================================"
echo "Python: $PYTHON"
echo "DYLD_LIBRARY_PATH: $DYLD_LIBRARY_PATH"
echo ""

# 检查端口是否被占用
if lsof -ti:$PORT > /dev/null 2>&1; then
  echo "端口 $PORT 已被占用，尝试重用..."
  kill $(lsof -ti:$PORT) 2>/dev/null
  sleep 1
fi

# 延迟打开浏览器
(sleep 2 && open "http://localhost:$PORT") &

# 启动服务器
echo "启动服务器: http://localhost:$PORT"
echo "按 Ctrl+C 停止"
echo ""
exec "$PYTHON" "${SCRIPT_DIR}/sms_server.py"

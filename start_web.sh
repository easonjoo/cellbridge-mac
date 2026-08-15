#!/bin/bash
# EG25-G SMS Web Server 启动脚本
# 启动后自动打开浏览器 http://localhost:8080

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export DYLD_LIBRARY_PATH=/opt/homebrew/lib
PYTHON="/Users/zxd/.workbuddy/binaries/python/envs/default/bin/python3"
PORT=8080

echo "================================"
echo "  EG25-G 短信助手"
echo "================================"
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

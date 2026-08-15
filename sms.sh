#!/bin/bash
# EG25-G SMS Tool 启动脚本
# Usage: ./sms.sh status | send <phone> <msg> | list | read <idx> | delete <idx> | delete-all | monitor

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
export DYLD_LIBRARY_PATH=/opt/homebrew/lib
exec /Users/zxd/.workbuddy/binaries/python/envs/default/bin/python3 "${SCRIPT_DIR}/sms_tool.py" "$@"

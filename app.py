#!/usr/bin/env python3
"""
DJiPhone Kit - macOS 原生窗口版
在后台线程运行 Flask 服务，用 pywebview 开原生窗口。
"""
import os
import sys
import socket
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

BIND_HOST = "0.0.0.0"   # 允许局域网 iPhone 访问
PORT = 8080
URL = "http://127.0.0.1:8080"


def server_alive():
    """检查 8080 是否已有服务在跑（复用，避免端口冲突）"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        try:
            s.connect(("127.0.0.1", PORT))
            return True
        except OSError:
            return False


def wait_http_ready(timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if server_alive():
            return True
        time.sleep(0.3)
    return False


def main():
    if not server_alive():
        import sms_server  # noqa: F401  导入即注册所有路由

        def run_flask():
            sms_server.app.run(
                host=BIND_HOST, port=PORT, debug=False,
                threaded=True, use_reloader=False,
            )

        threading.Thread(target=run_flask, daemon=True).start()
        if not wait_http_ready():
            print("Flask 服务启动失败", file=sys.stderr)
            sys.exit(1)

    # 菜单栏状态项（信号/网速/流量）——可选增强，失败不影响主窗口
    try:
        import menubar
        menubar.install(port=PORT)
    except Exception as e:
        print("[app] 菜单栏初始化跳过:", e, file=sys.stderr)

    import webview

    webview.create_window(
        "DJiPhone Kit",
        URL,
        width=1240,
        height=840,
        min_size=(900, 600),
        background_color="#f5f5f7",
    )
    webview.start()


if __name__ == "__main__":
    main()

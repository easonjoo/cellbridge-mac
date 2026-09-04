#!/usr/bin/env python3
"""
DJiPhone Kit - macOS 菜单栏状态项
在系统菜单栏常驻显示：4G 信号格数 + 实时上下行网速。
下拉菜单：今日/本月流量、打开主窗口、清零流量、退出。
数据来自本机 Flask 服务（127.0.0.1:8080），每 2 秒刷新。
"""
import json
import threading
import urllib.request

from AppKit import (
    NSApplication, NSAttributedString, NSFont, NSColor,
    NSStatusBar, NSMenu, NSMenuItem, NSVariableStatusItemLength,
)
from Foundation import NSObject


def _fetch(path, timeout=2):
    """GET 本机 API，返回 dict 或 None"""
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080" + path, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _post(path, timeout=4):
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8080" + path, method="POST",
            data=b"{}", headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _fmt_rate(kb):
    if kb is None or kb <= 0:
        return "0K"
    if kb < 1:
        return "%dB" % int(kb * 1024)
    if kb < 1024:
        return "%dK" % round(kb)
    return "%.1fM" % (kb / 1024.0)


def _fmt_traffic(kb):
    kb = kb or 0
    if kb < 1024:
        return "%d KB" % round(kb)
    if kb < 1024 * 1024:
        return "%.1f MB" % (kb / 1024.0)
    return "%.2f GB" % (kb / 1024.0 / 1024.0)


class _MenuBarController(NSObject):
    """主线程上的 UI 控制器（选择器都通过 performSelectorOnMainThread 调用）"""

    def initWithPort_(self, port):
        self = NSObject.init(self)
        if self is None:
            return None
        self.port = port
        self.base = "http://127.0.0.1:%d" % port
        return self

    # ---- UI 创建（主线程） ----
    def buildUI(self):
        item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.item = item
        menu = NSMenu.alloc().init()

        # 流量信息行（动态更新标题）
        self.trafficItem = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "流量读取中…", None, "")
        self.trafficItem.setEnabled_(False)
        menu.addItem_(self.trafficItem)

        menu.addItem_(NSMenuItem.separatorItem())

        openItem = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "打开主窗口", "showWindow:", "")
        openItem.setTarget_(self)
        menu.addItem_(openItem)

        resetItem = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "清零流量统计", "resetTraffic:", "")
        resetItem.setTarget_(self)
        menu.addItem_(resetItem)

        menu.addItem_(NSMenuItem.separatorItem())

        quitItem = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "退出 DJiPhone Kit", "quitApp:", "")
        quitItem.setTarget_(self)
        menu.addItem_(quitItem)

        item.setMenu_(menu)
        self.setBarData_(("", 0, 0, 0))

    # ---- 标题更新（主线程） ----
    def setBarData_(self, args):
        op, level, down_kb, up_kb = args
        from AppKit import NSMutableAttributedString
        # 信号条：▁▃▅▇ 四格，点亮的格子用绿色
        chars = ["▁", "▃", "▅", "▇"]
        combined = NSMutableAttributedString.alloc().init()
        for i in range(4):
            color = NSColor.systemGreenColor() if i < level else NSColor.tertiaryLabelColor()
            s = NSAttributedString.alloc().initWithString_attributes_(
                chars[i], {
                    "NSFont": NSFont.systemFontOfSize_(11.0),
                    "NSColor": color,
                })
            combined.appendAttributedString_(s)
        sep = NSAttributedString.alloc().initWithString_attributes_(
            "  ↓%s ↑%s" % (_fmt_rate(down_kb), _fmt_rate(up_kb)), {
                "NSFont": NSFont.monospacedDigitSystemFontOfSize_weight_(10.5, 0.0),
                "NSColor": NSColor.labelColor(),
            })
        combined.appendAttributedString_(sep)
        self.item.button().setAttributedTitle_(combined)

    def setTrafficInfo_(self, text):
        self.trafficItem.setTitle_(text)

    # ---- 菜单动作 ----
    def showWindow_(self, sender):
        try:
            import webview
            if webview.windows:
                w = webview.windows[0]
                w.show()
                w.restore()
        except Exception:
            pass

    def resetTraffic_(self, sender):
        _post("/api/data-usage/reset")

    def quitApp_(self, sender):
        try:
            NSApplication.sharedApplication().terminate_(None)
        except Exception:
            import os
            os._exit(0)


def install(port=8080):
    """
    在主线程创建菜单栏状态项 + 后台刷新线程。
    必须在 webview.start() 之前调用（主线程）。
    返回 controller；失败返回 None（菜单栏为可选增强，不应影响主程序）。
    """
    try:
        ctrl = _MenuBarController.alloc().initWithPort_(port)
        ctrl.performSelectorOnMainThread_withObject_waitUntilDone_(
            "buildUI", None, True)

        def refresh_loop():
            import time
            time.sleep(2)
            while True:
                st = _fetch("/api/status")
                sp = _fetch("/api/speed")
                try:
                    dbm = st.get("signal_dbm") if st else None
                    op = st.get("operator_name") or "" if st else ""
                    level = 0
                    if dbm is not None:
                        level = 4 if dbm > -85 else 3 if dbm > -95 else 2 if dbm > -105 else 1
                except Exception:
                    op, level = "", 0
                down = up = 0.0
                traffic_text = "流量数据读取中…"
                try:
                    if sp and sp.get("ok"):
                        today = (sp.get("today") or {})
                        month = (sp.get("month") or {})
                        down, up = float(sp.get("down") or 0), float(sp.get("up") or 0)
                        t_kb = (today.get("recv_kb") or 0) + (today.get("sent_kb") or 0)
                        m_kb = (month.get("recv_kb") or 0) + (month.get("sent_kb") or 0)
                        traffic_text = "今日 %s   ·   本月 %s" % (_fmt_traffic(t_kb), _fmt_traffic(m_kb))
                except Exception:
                    pass
                ctrl.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "setBarData:", (op, level, down, up), False)
                ctrl.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "setTrafficInfo_", traffic_text, False)
                time.sleep(2)

        threading.Thread(target=refresh_loop, daemon=True, name="menubar-refresh").start()
        return ctrl
    except Exception as e:
        print("[menubar] 初始化失败（不影响主程序）:", e, file=__import__("sys").stderr)
        return None

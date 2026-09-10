#!/usr/bin/env python3
"""at_probe.py — 临时 AT 查询：QPCMV/QDAI/网络制式（诊断 CS 语音静音问题）"""
import sys, time
import usb.core, usb.util

VID, PID = 0x2CA3, 0x4006
AT_IF, EP_IN, EP_OUT = 2, 0x84, 0x03

dev = usb.core.find(idVendor=VID, idProduct=PID)
if dev is None:
    print("模块未找到"); sys.exit(1)
try:
    if dev.is_kernel_driver_active(AT_IF):
        dev.detach_kernel_driver(AT_IF)
except Exception:
    pass
usb.util.claim_interface(dev, AT_IF)

def drain(t=0.4):
    out = b""
    while True:
        try:
            chunk = bytes(dev.read(EP_IN, 1024, timeout=int(t*1000)))
        except usb.core.USBError:
            break
        if not chunk:
            break
        out += chunk
        t = 0.15
    return out.decode("utf-8", "replace")

def at(cmd, wait=1.0):
    dev.write(EP_OUT, (cmd + "\r").encode(), timeout=3000)
    time.sleep(wait)
    return drain(0.5)

print(drain(0.3) or "(无残留)")
for cmd in ["ATE0", "AT+QPCMV=1,0", "AT+QPCMV?"]:
    print(f"\n>>> {cmd}")
    print(at(cmd).strip() or "(无响应)")

# 拨测电话并抓 UAC 采样验证非零
print("\n>>> ATD10010;")
print(at("ATD10010;", wait=3).strip() or "(无响应)")
os._exit(0)

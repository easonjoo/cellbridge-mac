#!/usr/bin/env python3
"""probe_at_direct.py — 直接通过 USB 探测 AT 通道，清理卡住的呼叫/AT 状态。"""
import sys, time
import usb.core, usb.util

VID, PID = 0x2CA3, 0x4006
AT_IF, EP_IN, EP_OUT = 2, 0x84, 0x03

dev = usb.core.find(idVendor=VID, idProduct=PID)
if dev is None:
    print("未找到模块"); sys.exit(1)
try:
    if dev.is_kernel_driver_active(AT_IF):
        dev.detach_kernel_driver(AT_IF)
except Exception:
    pass
usb.util.claim_interface(dev, AT_IF)
print("已占用 USB AT 接口")

# 1) 排干残留数据（最多 2 秒）
t0 = time.time()
residual = b""
while time.time() - t0 < 2:
    try:
        chunk = bytes(dev.read(EP_IN, 1024, timeout=200))
        residual += chunk
    except usb.core.USBError:
        break
if residual:
    print(f"残留数据 {len(residual)}B: {residual[:200]!r}")
else:
    print("无残留数据")

def at(cmd, wait=1.5):
    dev.write(EP_OUT, (cmd + "\r").encode(), timeout=3000)
    out, t0 = b"", time.time()
    while time.time() - t0 < wait:
        try:
            out += bytes(dev.read(EP_IN, 1024, timeout=150))
        except usb.core.USBError:
            pass
        if out.endswith(b"\r\n") or b"OK" in out or b"ERROR" in out:
            break
    return out.decode(errors="replace").strip()

for cmd in ["AT", "ATE0", "AT+CLCC", "AT+CREG?", "AT+CPAS", "AT+CMGF=1", "AT+CMGS=?"]:
    r = at(cmd)
    print(f">>> {cmd}\n{r or '(无响应)'}")

# 2) 若有活动/挂起呼叫，强制挂断清理
r = at("ATH")
print(">>> ATH\n" + (r or "(无响应)"))
time.sleep(1)
r = at("AT+CLCC")
print(">>> AT+CLCC（清理后）\n" + (r or "(无响应)"))
os_exit = None

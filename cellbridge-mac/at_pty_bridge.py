#!/usr/bin/env python3
"""
at_pty_bridge.py — 把 QDC507 的 USB AT 通道桥接为 macOS PTY 串口。

CellBridge 网关需要一个串口设备（/dev/cu.*）收发 AT 指令与 URC（RING 等）。
macOS 不会为该模块的 CDC-AT 接口创建串口节点，本脚本用 pty.openpty()
创建一对主从终端：网关打开从端（/dev/ttysNNN），我们把字节双向搬运到
USB bulk 端点。URC（RING/+CRING/+CMT 等）自然透传。

用法：python3 at_pty_bridge.py
  stdout 第一行输出 PTY 从端路径（供启动脚本捕获），其余日志走 stderr。
  注意：与 DJiPhone Kit App 互斥——两者都会占用 USB AT 接口（interface 2）。
"""
import os
import pty
import sys
import time
import threading

import usb.core
import usb.util

VID, PID = 0x2CA3, 0x4006
AT_IF = 2
EP_IN = 0x84
EP_OUT = 0x03


def log(msg):
    sys.stderr.write(f'[at-pty] {msg}\n')
    sys.stderr.flush()


def open_usb():
    while True:
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is not None:
            try:
                try:
                    if dev.is_kernel_driver_active(AT_IF):
                        dev.detach_kernel_driver(AT_IF)
                except Exception:
                    pass
                usb.util.claim_interface(dev, AT_IF)
                log('USB AT 接口已占用')
                return dev
            except Exception as e:
                log(f'占用 USB 接口失败（2s 后重试）: {e}')
        else:
            log('未找到模块（2s 后重试）')
        time.sleep(2)


def main():
    master_fd, slave_fd = pty.openpty()
    slave_path = os.ttyname(slave_fd)
    os.close(slave_fd)
    print(slave_path, flush=True)  # 启动脚本读这一行
    log(f'PTY 从端: {slave_path}')

    dev = open_usb()

    def usb2pty():
        while True:
            try:
                chunk = bytes(dev.read(EP_IN, 512, timeout=300))
            except usb.core.USBError:
                continue
            except Exception:
                time.sleep(1)
                continue
            if chunk:
                try:
                    os.write(master_fd, chunk)
                except OSError:
                    return  # 网关已关闭从端

    def pty2usb():
        while True:
            try:
                data = os.read(master_fd, 4096)
            except OSError:
                return
            if not data:
                time.sleep(0.05)
                continue
            try:
                dev.write(EP_OUT, data, timeout=3000)
            except Exception as e:
                log(f'USB 写失败: {e}')
                time.sleep(0.5)

    threading.Thread(target=usb2pty, daemon=True).start()
    threading.Thread(target=pty2usb, daemon=True).start()

    log('桥接运行中，Ctrl+C 退出')
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    # 跳过 libusb 析构（macOS 上退出时会触发 refcnt 断言崩溃）
    os._exit(0)


if __name__ == '__main__':
    main()

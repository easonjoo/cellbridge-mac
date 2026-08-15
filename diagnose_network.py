#!/usr/bin/env python3
"""EG25-G 网络配置诊断脚本 — 检查 APN/PDP/IPv4/IPv6/USB 网络模式"""
import time
import usb.core
import usb.util

VENDOR_ID = 0x2CA3
PRODUCT_ID = 0x4006
AT_INTERFACE = 2
EP_BULK_IN = 0x84
EP_BULK_OUT = 0x03


def send_at(dev, command, timeout=5000):
    data = (command + '\r').encode('utf-8')
    dev.write(EP_BULK_OUT, data, timeout=timeout)
    response = b''
    deadline = time.time() + timeout / 1000.0
    while time.time() < deadline:
        try:
            chunk = dev.read(EP_BULK_IN, 512, timeout=1000)
            response += bytes(chunk)
            text = response.decode('utf-8', errors='replace')
            if 'OK' in text or 'ERROR' in text or 'CME ERROR' in text:
                break
        except usb.core.USBError:
            if 'OK' in response.decode('utf-8', errors='replace'):
                break
            continue
    return response.decode('utf-8', errors='replace').strip()


def main():
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        print("❌ 未找到 EG25-G 模块")
        return

    try:
        try:
            if dev.is_kernel_driver_active(AT_INTERFACE):
                dev.detach_kernel_driver(AT_INTERFACE)
        except Exception:
            pass
        usb.util.claim_interface(dev, AT_INTERFACE)
    except Exception as e:
        print(f"❌ 无法占用 USB 接口: {e}")
        return

    commands = [
        ("APN/PDP 上下文", "AT+CGDCONT?"),
        ("运营商信息", "AT+COPS?"),
        ("网络注册状态", "AT+CREG?"),
        ("PS 域附着状态", "AT+CGREG?"),
        ("PDP 激活状态", "AT+CGACT?"),
        ("分配的 IP 地址", "AT+CGPADDR"),
        ("USB 网络模式", "AT+QCFG=\"usbnet\""),
        ("USB 配置", "AT+QCFG=\"usbcfg\""),
        ("数据连接状态", "AT+QNETDEVSTATUS=1"),
    ]

    for name, cmd in commands:
        try:
            resp = send_at(dev, cmd)
            print(f"\n===== {name} ({cmd}) =====")
            print(resp)
        except Exception as e:
            print(f"\n===== {name} ({cmd}) =====")
            print(f"  ❌ 发送失败: {e}")
        time.sleep(0.3)

    # 释放接口
    try:
        usb.util.release_interface(dev, AT_INTERFACE)
        try:
            dev.attach_kernel_driver(AT_INTERFACE)
        except Exception:
            pass
    except Exception:
        pass


if __name__ == '__main__':
    main()

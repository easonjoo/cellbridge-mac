#!/usr/bin/env python3
"""EG25-G 重启数据连接，修复 RNDIS DHCP 不分配 IPv4 的问题"""
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


def connect():
    dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
    if dev is None:
        return None
    try:
        try:
            if dev.is_kernel_driver_active(AT_INTERFACE):
                dev.detach_kernel_driver(AT_INTERFACE)
        except Exception:
            pass
        usb.util.claim_interface(dev, AT_INTERFACE)
    except Exception:
        return None
    return dev


def main():
    dev = connect()
    if dev is None:
        print("❌ 未找到模块")
        return

    print("发送 AT+CFUN=1,1 重启模块数据连接...")
    try:
        # 重启模块：CFUN=1,1 会重新初始化 RNDIS DHCP 服务器
        resp = send_at(dev, 'AT+CFUN=1,1', timeout=8000)
        print(f"响应: {resp}")
    except Exception as e:
        print(f"发送重启指令时出错（正常，模块正在重启）: {e}")

    # 释放接口，等待模块重启
    try:
        usb.util.release_interface(dev, AT_INTERFACE)
        try:
            dev.attach_kernel_driver(AT_INTERFACE)
        except Exception:
            pass
        usb.util.dispose_resources(dev)
    except Exception:
        pass

    print("等待模块重启（60 秒）...")
    time.sleep(60)

    # 重新连接并验证
    dev2 = connect()
    if dev2 is None:
        print("⚠️ 模块重启后暂未重新枚举，可能需要更多时间")
        return

    print("\n===== 重启后验证 =====")
    for name, cmd in [
        ("网络注册", "AT+CREG?"),
        ("PDP 激活", "AT+CGACT?"),
        ("分配的 IP", "AT+CGPADDR"),
    ]:
        try:
            resp = send_at(dev2, cmd)
            print(f"{name} ({cmd}): {resp}")
        except Exception as e:
            print(f"{name}: 失败 {e}")
        time.sleep(0.3)

    try:
        usb.util.release_interface(dev2, AT_INTERFACE)
        try:
            dev2.attach_kernel_driver(AT_INTERFACE)
        except Exception:
            pass
    except Exception:
        pass


if __name__ == '__main__':
    main()

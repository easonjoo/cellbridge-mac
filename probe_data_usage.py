#!/usr/bin/env python3
"""探测 EG25-G 模块的流量统计相关 AT 指令支持情况"""
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
        print("未找到模块")
        return
    try:
        try:
            if dev.is_kernel_driver_active(AT_INTERFACE):
                dev.detach_kernel_driver(AT_INTERFACE)
        except Exception:
            pass
        usb.util.claim_interface(dev, AT_INTERFACE)
    except Exception as e:
        print(f"占用接口失败: {e}")
        return

    # 探测流量统计相关指令
    probes = [
        ("数据统计配置", "AT+QGDATACFG?"),
        ("数据统计(读取)", "AT+QGDATACFG"),
        ("SIM 卡类型(确认卡类型)", "AT+CIMI"),
        ("ICCID(卡唯一标识)", "AT+QCCID"),
    ]
    for name, cmd in probes:
        try:
            resp = send_at(dev, cmd, timeout=4000)
            print(f"\n===== {name} ({cmd}) =====")
            print(resp)
        except Exception as e:
            print(f"\n===== {name} ({cmd}) =====")
            print(f"  失败: {e}")
        time.sleep(0.3)

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

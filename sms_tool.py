#!/usr/bin/env python3
"""
EG25-G SMS Tool — 通过 USB AT 指令收发短信
适用于 macOS 上无串口驱动的 Quectel EG25-G 模组

Usage:
  python sms_tool.py status          # 查询模组状态和网络注册
  python sms_tool.py send <phone> <message>   # 发送短信
  python sms_tool.py list            # 列出所有短信
  python sms_tool.py read <index>    # 读取指定短信
  python sms_tool.py delete <index>  # 删除指定短信
  python sms_tool.py delete-all      # 删除所有短信
  python sms_tool.py monitor         # 实时监控收到的短信
"""
import sys
import time
import usb.core
import usb.util
import threading

# EG25-G USB identifiers
VENDOR_ID = 0x2CA3
PRODUCT_ID = 0x4006
AT_INTERFACE = 2  # Primary AT command interface

# Endpoint addresses for interface 2
EP_BULK_IN = 0x84
EP_BULK_OUT = 0x03
EP_INTERRUPT_IN = 0x85


class EG25GModem:
    """Quectel EG25-G USB modem AT command interface."""

    def __init__(self):
        self.dev = None
        self.interface_num = AT_INTERFACE

    def connect(self):
        """Find and claim the EG25-G device."""
        self.dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
        if self.dev is None:
            raise RuntimeError("EG25-G device not found")

        # Detach kernel driver if active
        try:
            if self.dev.is_kernel_driver_active(self.interface_num):
                self.dev.detach_kernel_driver(self.interface_num)
        except NotImplementedError:
            pass  # macOS may not support this

        usb.util.claim_interface(self.dev, self.interface_num)
        return self

    def disconnect(self):
        """Release the device."""
        if self.dev:
            try:
                usb.util.release_interface(self.dev, self.interface_num)
            except Exception:
                pass
            try:
                self.dev.attach_kernel_driver(self.interface_num)
            except Exception:
                pass
            usb.util.dispose_resources(self.dev)

    def __enter__(self):
        return self.connect()

    def __exit__(self, *args):
        self.disconnect()

    def send_at(self, command, timeout=5000, wait_ok=True):
        """Send an AT command and return the response string."""
        data = (command + '\r').encode('utf-8')
        self.dev.write(EP_BULK_OUT, data, timeout=timeout)

        # Read response
        response = b''
        deadline = time.time() + timeout / 1000.0
        while time.time() < deadline:
            try:
                chunk = self.dev.read(EP_BULK_IN, 512, timeout=1000)
                response += bytes(chunk)
                text = response.decode('utf-8', errors='replace')
                if wait_ok and ('OK' in text or 'ERROR' in text or 'CME ERROR' in text):
                    break
                if not wait_ok and '\r\n' in text:
                    # For commands that return data (like CMGS prompt)
                    break
            except usb.core.USBError:
                if 'OK' in response.decode('utf-8', errors='replace'):
                    break
                continue

        return response.decode('utf-8', errors='replace').strip()

    def send_at_raw(self, data, timeout=10000):
        """Send raw bytes and read response. Used for CMGS (SMS send)."""
        self.dev.write(EP_BULK_OUT, data, timeout=timeout)
        response = b''
        deadline = time.time() + timeout / 1000.0
        while time.time() < deadline:
            try:
                chunk = self.dev.read(EP_BULK_IN, 512, timeout=2000)
                response += bytes(chunk)
                text = response.decode('utf-8', errors='replace')
                if '+CMGS:' in text and 'OK' in text:
                    break
                if 'ERROR' in text:
                    break
            except usb.core.USBError:
                continue
        return response.decode('utf-8', errors='replace').strip()

    def send_sms(self, phone, message):
        """Send an SMS message."""
        # Set text mode
        resp = self.send_at('AT+CMGF=1')
        if 'OK' not in resp:
            return False, f"Failed to set SMS text mode: {resp}"

        # Set GSM character set
        resp = self.send_at('AT+CSCS="GSM"')
        if 'OK' not in resp:
            return False, f"Failed to set character set: {resp}"

        # Set SMS service center address to auto
        self.send_at('AT+CSCA?')

        # Send the message: AT+CMGS="<phone>" then message + Ctrl+Z
        cmd = f'AT+CMGS="{phone}"\r'
        self.dev.write(EP_BULK_OUT, cmd.encode('utf-8'), timeout=5000)

        # Wait for the ">" prompt
        time.sleep(0.5)
        prompt = b''
        try:
            prompt = bytes(self.dev.read(EP_BULK_IN, 512, timeout=2000))
        except usb.core.USBError:
            pass
        prompt_text = prompt.decode('utf-8', errors='replace')

        if '>' not in prompt_text:
            # Try reading more
            try:
                prompt += bytes(self.dev.read(EP_BULK_IN, 512, timeout=2000))
                prompt_text = prompt.decode('utf-8', errors='replace')
            except usb.core.USBError:
                pass

        if '>' not in prompt_text:
            return False, f"No SMS prompt received. Response: {prompt_text}"

        # Send message content + Ctrl+Z (0x1A)
        msg_data = message.encode('utf-8') + b'\x1a'
        resp = self.send_at_raw(msg_data, timeout=30000)

        if '+CMGS:' in resp and 'OK' in resp:
            # Extract message reference
            ref = ''
            for line in resp.split('\n'):
                if '+CMGS:' in line:
                    ref = line.strip()
                    break
            return True, f"SMS sent successfully! {ref}"
        else:
            return False, f"SMS send failed: {resp}"

    def list_sms(self, stat="ALL"):
        """List SMS messages. stat: ALL, REC UNREAD, REC READ, STO UNSENT, STO SENT."""
        # Set text mode
        self.send_at('AT+CMGF=1')
        self.send_at('AT+CSCS="GSM"')

        resp = self.send_at(f'AT+CMGL="{stat}"', timeout=10000)

        messages = []
        lines = resp.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith('+CMGL:'):
                # Parse: +CMGL: <index>,<stat>,<oa/da>,[<alpha>],[<scts>],<tooa/toda>,<length>
                parts = line.split(',', 5)
                if len(parts) >= 4:
                    idx = parts[0].replace('+CMGL:', '').strip()
                    status = parts[1].strip().strip('"')
                    sender = parts[2].strip().strip('"')
                    timestamp = parts[4].strip().strip('"') if len(parts) > 4 else ''

                    # Next line is the message content
                    content = ''
                    if i + 1 < len(lines):
                        content = lines[i + 1].strip()
                        i += 1

                    messages.append({
                        'index': idx,
                        'status': status,
                        'sender': sender,
                        'timestamp': timestamp,
                        'content': content,
                    })
            i += 1

        return messages

    def read_sms(self, index):
        """Read SMS at given index."""
        self.send_at('AT+CMGF=1')
        self.send_at('AT+CSCS="GSM"')
        resp = self.send_at(f'AT+CMGR={index}', timeout=5000)

        lines = resp.split('\n')
        for i, line in enumerate(lines):
            if line.strip().startswith('+CMGR:'):
                parts = line.strip().split(',', 4)
                status = parts[0].replace('+CMGR:', '').strip().strip('"') if len(parts) > 0 else ''
                sender = parts[1].strip().strip('"') if len(parts) > 1 else ''
                timestamp = parts[3].strip().strip('"') if len(parts) > 3 else ''
                content = lines[i + 1].strip() if i + 1 < len(lines) else ''
                return {
                    'index': index,
                    'status': status,
                    'sender': sender,
                    'timestamp': timestamp,
                    'content': content,
                }
        return None

    def delete_sms(self, index):
        """Delete SMS at given index."""
        resp = self.send_at(f'AT+CMGD={index}', timeout=5000)
        return 'OK' in resp

    def delete_all_sms(self):
        """Delete all read SMS (delflag=1 deletes all read messages)."""
        resp = self.send_at('AT+CMGD=1,4', timeout=5000)
        return 'OK' in resp

    def get_status(self):
        """Query modem status, network registration, signal quality."""
        results = {}

        results['manufacturer'] = self.send_at('ATI')
        results['model'] = self.send_at('AT+GMM')
        results['imei'] = self.send_at('AT+GSN')
        results['sim_status'] = self.send_at('AT+CPIN?')
        results['signal_quality'] = self.send_at('AT+CSQ')
        results['network_reg'] = self.send_at('AT+CREG?')
        results['operator'] = self.send_at('AT+COPS?')
        results['sms_service_center'] = self.send_at('AT+CSCA?')
        results['sms_format'] = self.send_at('AT+CMGF?')

        return results

    def monitor_sms(self):
        """Monitor for incoming SMS in real-time."""
        print("Monitoring for incoming SMS... (Ctrl+C to stop)")
        print("-" * 60)

        # Enable new message notification
        self.send_at('AT+CMGF=1')
        self.send_at('AT+CSCS="GSM"')
        self.send_at('AT+CNMI=2,1,0,0,0')

        print("Waiting for new messages...")

        while True:
            try:
                data = bytes(self.dev.read(EP_BULK_IN, 512, timeout=5000))
                text = data.decode('utf-8', errors='replace')

                if '+CMTI:' in text:
                    # New message indication: +CMTI: "SM",<index>
                    for line in text.split('\n'):
                        if '+CMTI:' in line:
                            print(f"\n[NEW SMS] {line.strip()}")
                            # Extract index and read the message
                            try:
                                idx = line.split(',')[1].strip()
                                msg = self.read_sms(idx)
                                if msg:
                                    print(f"  From: {msg['sender']}")
                                    print(f"  Time: {msg['timestamp']}")
                                    print(f"  Content: {msg['content']}")
                                    print("-" * 60)
                            except (IndexError, ValueError):
                                pass
                elif text.strip() and not text.strip().startswith('AT'):
                    # Print any other unsolicited responses
                    pass

            except usb.core.USBError:
                continue
            except KeyboardInterrupt:
                print("\nStopped monitoring.")
                break


def print_status(status):
    """Pretty print modem status."""
    print("=" * 50)
    print("  EG25-G Modem Status")
    print("=" * 50)

    # Parse and display key info
    for key, raw in status.items():
        lines = [l.strip() for l in raw.split('\n') if l.strip() and not l.strip().startswith('AT')]
        value = ' '.join(lines) if lines else 'N/A'

        labels = {
            'manufacturer': 'Manufacturer',
            'model': 'Model',
            'imei': 'IMEI',
            'sim_status': 'SIM Status',
            'signal_quality': 'Signal Quality',
            'network_reg': 'Network Registration',
            'operator': 'Operator',
            'sms_service_center': 'SMS Center',
            'sms_format': 'SMS Format',
        }
        label = labels.get(key, key)
        print(f"  {label:.<25s} {value}")

    # Decode signal quality
    for raw in [status.get('signal_quality', '')]:
        for line in raw.split('\n'):
            if '+CSQ:' in line:
                try:
                    parts = line.split(':')
                    values = parts[1].split(',')
                    rssi = int(values[0].strip())
                    ber = int(values[1].strip())
                    if rssi == 99:
                        signal_str = "Unknown"
                    else:
                        dbm = -113 + 2 * rssi
                        signal_str = f"{rssi} (≈{dbm} dBm)"
                    print(f"  {'Signal Strength':.<25s} {signal_str}")
                    print(f"  {'Bit Error Rate':.<25s} {ber}")
                except (IndexError, ValueError):
                    pass

    print("=" * 50)


def print_messages(messages):
    """Pretty print SMS messages list."""
    if not messages:
        print("No messages found.")
        return

    print(f"Found {len(messages)} message(s):")
    print("-" * 60)
    for msg in messages:
        print(f"  [{msg['index']}] {msg['status']}")
        print(f"      From: {msg['sender']}")
        if msg['timestamp']:
            print(f"      Time: {msg['timestamp']}")
        print(f"      Content: {msg['content']}")
        print("-" * 60)


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    action = sys.argv[1].lower()

    if action == 'status':
        with EG25GModem() as modem:
            status = modem.get_status()
            print_status(status)

    elif action == 'send':
        if len(sys.argv) < 4:
            print("Usage: python sms_tool.py send <phone> <message>")
            sys.exit(1)
        phone = sys.argv[2]
        message = ' '.join(sys.argv[3:])
        with EG25GModem() as modem:
            ok, result = modem.send_sms(phone, message)
            print(result)

    elif action == 'list':
        with EG25GModem() as modem:
            messages = modem.list_sms("ALL")
            print_messages(messages)

    elif action == 'read':
        if len(sys.argv) < 3:
            print("Usage: python sms_tool.py read <index>")
            sys.exit(1)
        idx = sys.argv[2]
        with EG25GModem() as modem:
            msg = modem.read_sms(idx)
            if msg:
                print(f"  [{msg['index']}] {msg['status']}")
                print(f"      From: {msg['sender']}")
                print(f"      Time: {msg['timestamp']}")
                print(f"      Content: {msg['content']}")
            else:
                print(f"No message at index {idx}")

    elif action == 'delete':
        if len(sys.argv) < 3:
            print("Usage: python sms_tool.py delete <index>")
            sys.exit(1)
        idx = sys.argv[2]
        with EG25GModem() as modem:
            if modem.delete_sms(idx):
                print(f"Deleted message at index {idx}")
            else:
                print(f"Failed to delete message at index {idx}")

    elif action == 'delete-all':
        with EG25GModem() as modem:
            if modem.delete_all_sms():
                print("All messages deleted")
            else:
                print("Failed to delete messages")

    elif action == 'monitor':
        with EG25GModem() as modem:
            modem.monitor_sms()

    else:
        print(f"Unknown action: {action}")
        print(__doc__)
        sys.exit(1)


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""
EG25-G SMS Web Server
Flask 后端，提供短信收发 REST API + 前端页面
启动后访问 http://localhost:8080
"""
import os
import sys
import time
import json
import re
import threading
import uuid
import usb.core
import usb.util
import psutil
from flask import Flask, request, jsonify, send_file

# ─── EG25-G USB Config ───────────────────────────────────────────────
VENDOR_ID = 0x2CA3
PRODUCT_ID = 0x4006
AT_INTERFACE = 2
EP_BULK_IN = 0x84
EP_BULK_OUT = 0x03

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def normalize_phone(p):
    """规范化手机号:只保留数字和+。"""
    return ''.join(c for c in (p or '') if c.isdigit() or c == '+')


CALL_NUMBER_PATTERN = re.compile(r'^\+?\d{1,20}$')

# ─── ModemManager ────────────────────────────────────────────────────
class ModemManager:
    """持久连接、线程安全的 EG25-G 调制解调器管理器。"""

    def __init__(self):
        self.dev = None
        self._lock = threading.Lock()
        self._claimed = False

    def _connect(self):
        """查找并占用设备。"""
        self.dev = usb.core.find(idVendor=VENDOR_ID, idProduct=PRODUCT_ID)
        if self.dev is None:
            return False
        try:
            try:
                if self.dev.is_kernel_driver_active(AT_INTERFACE):
                    self.dev.detach_kernel_driver(AT_INTERFACE)
            except (NotImplementedError, Exception):
                pass
            usb.util.claim_interface(self.dev, AT_INTERFACE)
            self._claimed = True
            return True
        except Exception:
            self.dev = None
            return False

    def _disconnect(self):
        """释放设备。"""
        if self.dev:
            if self._claimed:
                try:
                    usb.util.release_interface(self.dev, AT_INTERFACE)
                except Exception:
                    pass
                self._claimed = False
            try:
                self.dev.attach_kernel_driver(AT_INTERFACE)
            except Exception:
                pass
            usb.util.dispose_resources(self.dev)
            self.dev = None

    def is_connected(self):
        """检查设备是否连接，未连接时尝试自动连接。"""
        with self._lock:
            if self.dev is not None and self._claimed:
                try:
                    cfg = self.dev.get_active_configuration()
                    return cfg is not None
                except Exception:
                    self._disconnect()
                    return False
            return self._connect()

    def _ensure(self):
        """确保已连接，否则尝试重连。"""
        if self.dev is not None and self._claimed:
            return True
        return self._connect()

    def send_at(self, command, timeout=5000, wait_ok=True):
        """发送 AT 指令并返回响应。"""
        with self._lock:
            if not self._ensure():
                raise RuntimeError("EG25-G 模块未连接")
            try:
                data = (command + '\r').encode('utf-8')
                self.dev.write(EP_BULK_OUT, data, timeout=timeout)
                response = b''
                deadline = time.time() + timeout / 1000.0
                while time.time() < deadline:
                    try:
                        chunk = self.dev.read(EP_BULK_IN, 512, timeout=1000)
                        response += bytes(chunk)
                        text = response.decode('utf-8', errors='replace')
                        if wait_ok and ('OK' in text or 'ERROR' in text or 'CME ERROR' in text):
                            break
                        if not wait_ok and '\r\n>' in text:
                            break
                    except usb.core.USBError:
                        if 'OK' in response.decode('utf-8', errors='replace'):
                            break
                        continue
                return response.decode('utf-8', errors='replace').strip()
            except usb.core.USBError as e:
                self._disconnect()
                raise RuntimeError(f"USB 通信失败: {e}")

    def send_sms(self, phone, message):
        """发送短信。"""
        with self._lock:
            if not self._ensure():
                raise RuntimeError("EG25-G 模块未连接")
            try:
                # 设置文本模式
                self.dev.write(EP_BULK_OUT, b'AT+CMGF=1\r', timeout=5000)
                self._drain(timeout=2000)
                self.dev.write(EP_BULK_OUT, b'AT+CSCS="GSM"\r', timeout=5000)
                self._drain(timeout=2000)

                # 发送 CMGS 命令
                cmd = f'AT+CMGS="{phone}"\r'.encode('utf-8')
                self.dev.write(EP_BULK_OUT, cmd, timeout=5000)
                time.sleep(0.5)

                # 等待 > 提示符
                prompt = b''
                try:
                    prompt = bytes(self.dev.read(EP_BULK_IN, 512, timeout=2000))
                except usb.core.USBError:
                    pass

                # 再读一次以防万一
                if b'>' not in prompt:
                    try:
                        prompt += bytes(self.dev.read(EP_BULK_IN, 512, timeout=2000))
                    except usb.core.USBError:
                        pass

                if b'>' not in prompt:
                    raise RuntimeError("未收到短信输入提示符 (>)")

                # 发送消息内容 + Ctrl+Z
                msg_data = message.encode('utf-8') + b'\x1a'
                self.dev.write(EP_BULK_OUT, msg_data, timeout=5000)

                # 读取发送结果
                response = b''
                deadline = time.time() + 30
                while time.time() < deadline:
                    try:
                        chunk = self.dev.read(EP_BULK_IN, 512, timeout=3000)
                        response += bytes(chunk)
                        text = response.decode('utf-8', errors='replace')
                        if '+CMGS:' in text and 'OK' in text:
                            break
                        if 'ERROR' in text:
                            break
                    except usb.core.USBError:
                        continue

                text = response.decode('utf-8', errors='replace')
                if '+CMGS:' in text and 'OK' in text:
                    ref = ''
                    for line in text.split('\n'):
                        if '+CMGS:' in line:
                            ref = line.strip()
                            break
                    return True, ref
                else:
                    return False, text.strip()
            except usb.core.USBError as e:
                self._disconnect()
                raise RuntimeError(f"USB 通信失败: {e}")

    def _drain(self, timeout=2000):
        """读取并丢弃缓冲区中的数据。"""
        try:
            self.dev.read(EP_BULK_IN, 512, timeout=timeout)
        except usb.core.USBError:
            pass

    def list_sms(self, stat="ALL"):
        """列出短信。"""
        # 先设置文本模式和字符集
        self.send_at('AT+CMGF=1')
        self.send_at('AT+CSCS="GSM"')
        resp = self.send_at(f'AT+CMGL="{stat}"', timeout=10000)
        messages = []
        lines = resp.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith('+CMGL:'):
                parts = self._parse_cmgl(line)
                if parts:
                    content = ''
                    if i + 1 < len(lines):
                        content = lines[i + 1].strip()
                        i += 1
                    parts['content'] = content
                    messages.append(parts)
            i += 1
        return messages

    def _parse_cmgl(self, line):
        """解析 +CMGL 行。"""
        try:
            rest = line[len('+CMGL:'):].strip()
            parts = self._split_csv(rest)
            return {
                'index': parts[0].strip(),
                'status': parts[1].strip().strip('"') if len(parts) > 1 else '',
                'sender': parts[2].strip().strip('"') if len(parts) > 2 else '',
                'timestamp': parts[4].strip().strip('"') if len(parts) > 4 else '',
            }
        except Exception:
            return None

    def _split_csv(self, s):
        """简单的 CSV 分割（处理引号内逗号）。"""
        result = []
        current = ''
        in_quotes = False
        for ch in s:
            if ch == '"':
                in_quotes = not in_quotes
                current += ch
            elif ch == ',' and not in_quotes:
                result.append(current)
                current = ''
            else:
                current += ch
        result.append(current)
        return result

    def read_sms(self, index):
        """读取指定索引的短信。"""
        self.send_at('AT+CMGF=1')
        self.send_at('AT+CSCS="GSM"')
        resp = self.send_at(f'AT+CMGR={index}', timeout=5000)
        lines = resp.split('\n')
        for i, line in enumerate(lines):
            if line.strip().startswith('+CMGR:'):
                rest = line.strip()[len('+CMGR:'):].strip()
                parts = self._split_csv(rest)
                content = lines[i + 1].strip() if i + 1 < len(lines) else ''
                return {
                    'index': str(index),
                    'status': parts[0].strip().strip('"') if len(parts) > 0 else '',
                    'sender': parts[1].strip().strip('"') if len(parts) > 1 else '',
                    'timestamp': parts[3].strip().strip('"') if len(parts) > 3 else '',
                    'content': content,
                }
        return None

    def delete_sms(self, index):
        """删除指定索引的短信。"""
        resp = self.send_at(f'AT+CMGD={index}', timeout=5000)
        return 'OK' in resp

    def delete_all_sms(self):
        """删除所有短信。"""
        resp = self.send_at('AT+CMGD=1,4', timeout=5000)
        return 'OK' in resp

    def get_status(self):
        """查询模组状态。"""
        result = {}
        result['connected'] = self.is_connected()
        if not result['connected']:
            return result

        try:
            result['manufacturer'] = self._clean(self.send_at('ATI'))
            result['model'] = self._clean(self.send_at('AT+GMM'))
            result['imei'] = self._clean(self.send_at('AT+GSN'))
            result['sim_status'] = self._clean(self.send_at('AT+CPIN?'))
            result['signal_quality'] = self._clean(self.send_at('AT+CSQ'))
            result['operator'] = self._clean(self.send_at('AT+COPS?'))
            result['sms_center'] = self._clean(self.send_at('AT+CSCA?'))

            # 解析信号质量
            result['signal_dbm'] = None
            for line in result.get('signal_quality', '').split('\n'):
                if '+CSQ:' in line:
                    try:
                        vals = line.split(':')[1].split(',')
                        rssi = int(vals[0].strip())
                        result['signal_rssi'] = rssi
                        result['signal_dbm'] = -113 + 2 * rssi if rssi != 99 else None
                    except (IndexError, ValueError):
                        pass

            # 解析运营商
            for line in result.get('operator', '').split('\n'):
                if '+COPS:' in line:
                    try:
                        vals = line.split(':')[1].split(',')
                        if len(vals) >= 3:
                            result['operator_name'] = vals[2].strip().strip('"')
                    except (IndexError, ValueError):
                        pass

            result['ok'] = True
        except Exception as e:
            result['ok'] = False
            result['error'] = str(e)
        return result

    def _clean(self, text):
        """清理 AT 响应：去掉命令回显和 OK。"""
        lines = []
        for line in text.split('\n'):
            line = line.strip()
            if line and not line.startswith('AT') and line != 'OK' and line != 'ERROR':
                lines.append(line)
        return '\n'.join(lines) if lines else ''


# ─── Sent Message Store (本地持久化已发送短信) ────────────────────────
class SentMessageStore:
    """将已发送短信保存到本地 JSON 文件，模拟手机的发件箱。"""

    def __init__(self, filepath):
        self.filepath = filepath
        self._lock = threading.Lock()
        self._messages = []
        self._load()

    def _load(self):
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                self._messages = json.load(f)
        except Exception:
            self._messages = []

    def _save(self):
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(self._messages, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def add(self, phone, content, ref=''):
        with self._lock:
            msg = {
                'id': f"sent_{int(time.time() * 1000)}",
                'direction': 'sent',
                'phone': phone,
                'sender': phone,
                'content': content,
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                'ref': ref,
            }
            self._messages.append(msg)
            self._save()
            return msg

    def get_all(self):
        with self._lock:
            return list(self._messages)

    def delete(self, msg_id):
        with self._lock:
            before = len(self._messages)
            self._messages = [m for m in self._messages if m['id'] != msg_id]
            self._save()
            return len(self._messages) < before

    def delete_all(self):
        with self._lock:
            self._messages = []
            self._save()
            return True


# ─── Call Record Store ───────────────────────────────────────────────

class CallRecordStore:
    """持久化通话记录到本地 JSON 文件,模拟手机通话记录。"""

    def __init__(self, filepath):
        self.filepath = filepath
        self._lock = threading.Lock()
        self._records = []
        self._load()

    def _load(self):
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                records = json.load(f)
                self._records = records if isinstance(records, list) else []
        except Exception:
            self._records = []

    def _save(self):
        temp_path = f"{self.filepath}.tmp"
        try:
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(self._records, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.filepath)
        except Exception:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
            raise

    def add(self, number, call_type, duration, direction):
        """添加一条通话记录。
        call_type: 'outgoing' / 'incoming' / 'missed'
        direction: 'out' / 'in'
        """
        with self._lock:
            record = {
                'id': f"call_{uuid.uuid4().hex}",
                'number': number,
                'type': call_type,
                'duration': duration,
                'direction': direction,
                'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
                'ts': time.time(),
            }
            self._records.append(record)
            # 保留最近 500 条
            if len(self._records) > 500:
                self._records = self._records[-500:]
            self._save()
            return record

    def get_all(self):
        with self._lock:
            return list(reversed(self._records))  # 最新的在前

    def delete(self, record_id):
        with self._lock:
            before = len(self._records)
            self._records = [r for r in self._records if r['id'] != record_id]
            self._save()
            return len(self._records) < before

    def delete_all(self):
        with self._lock:
            self._records = []
            self._save()
            return True


# ─── Network Speed Monitor ───────────────────────────────────────────
class SpeedMonitor:
    """监控 4G 模块网卡(en9)的实时上下行速率。"""

    def __init__(self, interface='en9'):
        self.interface = interface
        self._lock = threading.Lock()
        self._last_bytes = None  # (bytes_sent, bytes_recv, timestamp)
        self._history = []       # 保留最近 60 个采样点
        self._max_history = 60

    def _find_interface(self):
        """自动查找 4G 模块的网卡接口名。"""
        try:
            stats = psutil.net_io_counters(pernic=True)
            # 优先用 en9，找不到就找其他非 lo0/en0 的活跃接口
            if self.interface in stats:
                return self.interface
            for name in stats:
                if name.startswith('en') and name not in ('en0',) and int(stats[name].isup if hasattr(stats[name], 'isup') else True):
                    return name
        except Exception:
            pass
        return self.interface

    def get_speed(self):
        """返回当前上下行速率 (bytes/s) 及历史数据。"""
        with self._lock:
            iface = self._find_interface()
            try:
                stats = psutil.net_io_counters(pernic=True)
                if iface not in stats:
                    return {'ok': False, 'error': f'接口 {iface} 未找到', 'interface': iface}
                now = time.time()
                sent = stats[iface].bytes_sent
                recv = stats[iface].bytes_recv
                result = {
                    'ok': True,
                    'interface': iface,
                    'timestamp': now,
                }
                if self._last_bytes is not None:
                    prev_sent, prev_recv, prev_time = self._last_bytes
                    dt = now - prev_time
                    if dt > 0:
                        upload_speed = max(0, (sent - prev_sent) / dt)
                        download_speed = max(0, (recv - prev_recv) / dt)
                        result['upload_speed'] = round(upload_speed, 1)
                        result['download_speed'] = round(download_speed, 1)
                        result['total_sent'] = sent
                        result['total_recv'] = recv
                        # 记录历史
                        self._history.append({
                            't': now,
                            'up': round(upload_speed, 1),
                            'down': round(download_speed, 1),
                        })
                        if len(self._history) > self._max_history:
                            self._history.pop(0)
                        result['history'] = list(self._history[-30:])
                    else:
                        result['upload_speed'] = 0
                        result['download_speed'] = 0
                else:
                    result['upload_speed'] = 0
                    result['download_speed'] = 0
                    result['total_sent'] = sent
                    result['total_recv'] = recv
                self._last_bytes = (sent, recv, now)
                return result
            except Exception as e:
                return {'ok': False, 'error': str(e), 'interface': iface}


# ─── Data Usage Monitor (流量消耗统计) ────────────────────────────────
class DataUsageMonitor:
    """累计统计 4G 模块网卡(en9)的流量消耗，持久化到 data_usage.json。

    处理接口重置(拔插模块后 bytes 计数归零)：检测到当前计数小于上次采样时，
    先把当前接口生命周期内的量并入 accumulated，再以新计数为基线继续统计。
    """

    def __init__(self, interface='en9', filepath=None):
        self.interface = interface
        self.filepath = filepath or os.path.join(BASE_DIR, 'data_usage.json')
        self._lock = threading.Lock()
        self._data = {
            'start_ts': 0,
            'accumulated_sent': 0,
            'accumulated_recv': 0,
            'baseline_sent': 0,
            'baseline_recv': 0,
            'last_sent': 0,
            'last_recv': 0,
            'daily': {},
            'monthly': {},
        }
        self._load()

    def _load(self):
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                loaded = json.load(f)
                self._data.update(loaded)
        except Exception:
            pass

    def _save(self):
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _find_interface(self):
        try:
            stats = psutil.net_io_counters(pernic=True)
            if self.interface in stats:
                return self.interface
            for name in stats:
                if name.startswith('en') and name != 'en0':
                    return name
        except Exception:
            pass
        return self.interface

    def _read_counters(self):
        stats = psutil.net_io_counters(pernic=True)
        iface = self._find_interface()
        if iface not in stats:
            return iface, None
        return iface, (stats[iface].bytes_sent, stats[iface].bytes_recv)

    def tick(self):
        """采样一次并累计流量，返回当前统计。"""
        with self._lock:
            iface, counters = self._read_counters()
            if counters is None:
                return self._summary(iface, unavailable=True)
            sent, recv = counters
            now = time.time()

            if self._data['start_ts'] == 0:
                self._data['start_ts'] = now
                self._data['baseline_sent'] = sent
                self._data['baseline_recv'] = recv
                self._data['last_sent'] = sent
                self._data['last_recv'] = recv
                self._save()
                return self._summary(iface)

            # 接口重置：当前计数小于上次采样，说明 bytes 归零
            if sent < self._data['last_sent'] or recv < self._data['last_recv']:
                self._data['accumulated_sent'] += max(0, self._data['last_sent'] - self._data['baseline_sent'])
                self._data['accumulated_recv'] += max(0, self._data['last_recv'] - self._data['baseline_recv'])
                self._data['baseline_sent'] = sent
                self._data['baseline_recv'] = recv

            inc_sent = max(0, sent - self._data['last_sent'])
            inc_recv = max(0, recv - self._data['last_recv'])

            if inc_sent or inc_recv:
                day = time.strftime('%Y-%m-%d')
                month = time.strftime('%Y-%m')
                d = self._data['daily'].setdefault(day, {'sent': 0, 'recv': 0})
                d['sent'] += inc_sent
                d['recv'] += inc_recv
                m = self._data['monthly'].setdefault(month, {'sent': 0, 'recv': 0})
                m['sent'] += inc_sent
                m['recv'] += inc_recv

            self._data['last_sent'] = sent
            self._data['last_recv'] = recv
            self._save()
            return self._summary(iface)

    def _summary(self, iface, unavailable=False):
        cur_sent = max(0, self._data['last_sent'] - self._data['baseline_sent'])
        cur_recv = max(0, self._data['last_recv'] - self._data['baseline_recv'])
        total_sent = self._data['accumulated_sent'] + cur_sent
        total_recv = self._data['accumulated_recv'] + cur_recv
        day = time.strftime('%Y-%m-%d')
        month = time.strftime('%Y-%m')
        today = self._data['daily'].get(day, {'sent': 0, 'recv': 0})
        this_month = self._data['monthly'].get(month, {'sent': 0, 'recv': 0})
        return {
            'ok': not unavailable,
            'interface': iface,
            'unavailable': unavailable,
            'total_sent': total_sent,
            'total_recv': total_recv,
            'total': total_sent + total_recv,
            'today_sent': today['sent'],
            'today_recv': today['recv'],
            'today_total': today['sent'] + today['recv'],
            'month_sent': this_month['sent'],
            'month_recv': this_month['recv'],
            'month_total': this_month['sent'] + this_month['recv'],
            'start_ts': self._data['start_ts'],
            'daily': dict(self._data['daily']),
        }

    def get_summary(self):
        with self._lock:
            iface, counters = self._read_counters()
            if counters is None:
                return self._summary(iface, unavailable=True)
            sent, recv = counters
            # 计算当前实时总量（不写盘，只读）
            cur_sent = max(0, sent - self._data['baseline_sent'])
            cur_recv = max(0, recv - self._data['baseline_recv'])
            total_sent = self._data['accumulated_sent'] + cur_sent
            total_recv = self._data['accumulated_recv'] + cur_recv
            summary = self._summary(iface)
            summary['total_sent'] = total_sent
            summary['total_recv'] = total_recv
            summary['total'] = total_sent + total_recv
            return summary

    def reset(self):
        """清零所有统计，以当前计数为新基线。"""
        with self._lock:
            iface, counters = self._read_counters()
            sent = recv = 0
            if counters is not None:
                sent, recv = counters
            self._data.update({
                'start_ts': time.time(),
                'accumulated_sent': 0,
                'accumulated_recv': 0,
                'baseline_sent': sent,
                'baseline_recv': recv,
                'last_sent': sent,
                'last_recv': recv,
                'daily': {},
                'monthly': {},
            })
            self._save()
            return self._summary(iface, unavailable=counters is None)


# ─── Call Manager (通话状态管理) ─────────────────────────────────────
class CallManager:
    """后台轮询 AT+CLCC，检测来电和通话状态变化。"""

    CALL_STATES = {0: 'active', 1: 'held', 2: 'dialing', 3: 'alerting', 4: 'incoming', 5: 'waiting'}

    def __init__(self, modem, call_store=None):
        self.modem = modem
        self.call_store = call_store  # CallRecordStore 实例,用于保存通话记录
        self._lock = threading.Lock()
        self._action_lock = threading.Lock()
        self._state = 'idle'       # idle / ringing / dialing / alerting / active
        self._number = ''
        self._direction = ''       # 'in' / 'out'
        self._call_start = None
        self._thread = None
        self._running = False
        self._hangup_until = 0     # 挂断冷却期:此时间戳之前忽略模块残留状态
        self._last_call_record = None  # 缓存上一次通话信息用于记录
        self._call_recorded = False    # 当前通话是否已记录(防止重复/漏记)
        self._hungup_number = ''       # 刚挂断的号码,用于过滤模块固件残留状态

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def _poll_loop(self):
        while self._running:
            try:
                if self.modem.is_connected():
                    self._check()
            except Exception:
                pass
            time.sleep(2)

    def _check(self):
        """查询 AT+CLCC 并解析通话状态。"""
        with self._action_lock:
            self._check_locked()

    def _check_locked(self):
        """执行一次状态查询。调用者必须持有 _action_lock。"""
        if time.time() < self._hangup_until:
            return
        resp = self.modem.send_at('AT+CLCC', timeout=3000)
        has_ring = 'RING' in resp
        calls = []
        for line in resp.split('\n'):
            line = line.strip()
            if line.startswith('+CLCC:'):
                rest = line[len('+CLCC:'):].strip()
                parts = self.modem._split_csv(rest)
                if len(parts) >= 5:
                    try:
                        stat = int(parts[2].strip())
                    except ValueError:
                        continue
                    calls.append({
                        'dir': parts[1].strip(),
                        'stat': stat,
                        'number': parts[5].strip().strip('"') if len(parts) > 5 else '',
                        'state': self.CALL_STATES.get(stat, 'unknown'),
                    })
        with self._lock:
            if calls:
                c = calls[0]
                # 过滤:号码为空且 stat=0 的疑似残留状态(模组固件态机问题)
                if c['stat'] == 0 and not c['number'] and self._state != 'active':
                    return
                # 过滤:挂断后模块固件残留同号码的拨出通话状态
                # (EG25-G 固件 bug: ATH 后 AT+CLCC 仍报告 dialing/active)
                if self._hungup_number:
                    call_num = c.get('number', '')
                    if call_num and normalize_phone(call_num) == normalize_phone(self._hungup_number):
                        # 同号码的拨出通话残留 - 仅当当前状态已经是 idle 时跳过
                        if c['dir'] == '0' and c['stat'] in (0, 2, 3) and self._state == 'idle':
                            return
                    # 如果是新的来电(stat=4),清除挂断号码标记
                    if c['stat'] == 4:
                        self._hungup_number = ''
                # idle -> 非 idle 表示一通新电话。每一通都必须重置去重标记，
                # 否则连续未接来电只会保存第一条。
                if self._state == 'idle':
                    self._call_recorded = False
                    self._call_start = None
                    self._last_call_record = None
                self._direction = 'in' if c['dir'] == '1' else 'out'
                if c['number']:
                    self._number = c['number']
                if c['stat'] == 4:
                    self._state = 'ringing'
                elif c['stat'] == 2:
                    self._state = 'dialing'
                elif c['stat'] == 3:
                    self._state = 'alerting'
                elif c['stat'] == 0:
                    self._state = 'active'
                    if self._call_start is None:
                        self._call_start = time.time()
                else:
                    self._state = c['state']
            elif has_ring:
                if self._state not in ('ringing', 'active'):
                    if self._state == 'idle':
                        self._call_recorded = False
                        self._call_start = None
                        self._last_call_record = None
                    self._state = 'ringing'
                    self._direction = 'in'
            else:
                # 模组报告无通话 — 如果之前在通话中,记录通话结束
                if self._state in ('active', 'ringing', 'dialing', 'alerting'):
                    self._record_call_end()
                self._state = 'idle'
                self._call_start = None
                self._number = ''
                self._direction = ''
                self._hungup_number = ''  # 模组确认无通话,清除标记

    def get_status(self):
        with self._lock:
            duration = 0
            if self._call_start and self._state == 'active':
                duration = int(time.time() - self._call_start)
            return {
                'state': self._state,
                'number': self._number,
                'direction': self._direction,
                'duration': duration,
            }

    def dial(self, number):
        with self._action_lock:
            with self._lock:
                if self._state != 'idle':
                    raise ValueError('当前已有通话，请先挂断')
            # 清理模组可能遗留、但未被 CLCC 报告的通话状态。
            try:
                self.modem.send_at('ATH', timeout=3000)
            except Exception:
                pass
            time.sleep(0.2)
            with self._lock:
                self._state = 'dialing'
                self._number = number
                self._direction = 'out'
                self._call_start = None
                self._hangup_until = time.time() + 4
                self._call_recorded = False
                self._hungup_number = ''
                self._last_call_record = None
            try:
                resp = self.modem.send_at(f'ATD{number};', timeout=10000)
                ok = 'OK' in resp
            except Exception:
                ok = False
                raise
            finally:
                with self._lock:
                    self._hangup_until = 0
                    if not ok:
                        self._record_call_end()
                        self._reset_state_locked()
            return ok

    def answer(self):
        with self._action_lock:
            resp = self.modem.send_at('ATA', timeout=5000)
            ok = 'OK' in resp
            if ok:
                with self._lock:
                    self._state = 'active'
                    self._call_start = time.time()
                    self._call_recorded = False
                    self._hungup_number = ''
            return ok

    def hangup(self):
        """请求模组挂断；成功后结束会话并保存记录。"""
        with self._action_lock:
            accepted = False
            for command in ('AT+CHUP', 'ATH'):
                try:
                    resp = self.modem.send_at(command, timeout=3000)
                    if 'OK' in resp or 'NO CARRIER' in resp:
                        accepted = True
                except Exception:
                    pass
                time.sleep(0.15)

            with self._lock:
                # 轮询可能已先确认远端结束；这种情况也视为挂断成功。
                if self._state == 'idle':
                    return True
                if not accepted:
                    return False
                self._record_call_end()
                self._hungup_number = self._number or ''
                self._reset_state_locked()
                self._hangup_until = time.time() + 5
            return True

    def _record_call_end(self):
        """通话结束时保存通话记录。调用者应已持有 self._lock。"""
        # 防止重复记录
        if self._call_recorded:
            return
        if self._state in ('active', 'ringing', 'dialing', 'alerting'):
            duration = 0
            if self._call_start:
                duration = int(time.time() - self._call_start)
            # 判断通话类型
            if self._state == 'ringing' and self._direction == 'in':
                call_type = 'missed'   # 来电未接
            elif self._direction == 'in':
                call_type = 'incoming'  # 接听来电
            else:
                call_type = 'outgoing'  # 主动拨出
            number = self._number or '未知号码'
            # 如果是拨号但没接通(dialing/alerting状态挂断),也算拨出
            if self._direction == 'out' and self._state in ('dialing', 'alerting'):
                duration = 0
            record = {
                'number': number,
                'type': call_type,
                'duration': duration,
                'direction': self._direction or 'out',
            }
            self._last_call_record = record
            self._call_recorded = True  # 标记已记录
            # 保存到持久化存储
            if self.call_store:
                try:
                    saved = self.call_store.add(
                        number=record['number'],
                        call_type=record['type'],
                        duration=record['duration'],
                        direction=record['direction'],
                    )
                    print(f"[CallRecord] 保存通话记录: {record['type']} {record['number']} 时长={duration}s", flush=True)
                except Exception as e:
                    print(f"[CallRecord] 保存失败: {e}", flush=True)

    def send_dtmf(self, digit):
        with self._action_lock:
            resp = self.modem.send_at(f'AT+QLDTMF=100,"{digit}"', timeout=3000)
            return 'OK' in resp

    def _reset_state_locked(self):
        """清空当前会话。调用者必须持有 _lock。"""
        self._state = 'idle'
        self._call_start = None
        self._number = ''
        self._direction = ''

    def force_reset(self):
        """强制重置通话状态(用于模块固件残留状态无法清除时)。"""
        with self._action_lock:
            with self._lock:
                if self._number or self._direction:
                    self._record_call_end()
                self._reset_state_locked()
                self._call_recorded = False
                self._hungup_number = ''
                self._hangup_until = time.time() + 10  # 10 秒冷却期


# ─── Flask App ───────────────────────────────────────────────────────
app = Flask(__name__, static_folder=None)
modem = ModemManager()
speed_monitor = SpeedMonitor()
data_usage = DataUsageMonitor()
sent_store = SentMessageStore(os.path.join(BASE_DIR, 'sent_sms.json'))
call_store = CallRecordStore(os.path.join(BASE_DIR, 'call_history.json'))
call_manager = CallManager(modem, call_store=call_store)
if os.environ.get('SMS_HUB_DISABLE_BACKGROUND') != '1':
    call_manager.start()


def _data_usage_loop():
    """后台线程，每 5 秒采样一次流量，保证按天/按月归类准确。"""
    while True:
        try:
            data_usage.tick()
        except Exception:
            pass
        time.sleep(5)


_data_usage_thread = threading.Thread(target=_data_usage_loop, daemon=True)
_data_usage_thread.start()


@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'index.html'))


@app.route('/api/status')
def api_status():
    try:
        return jsonify(modem.get_status())
    except Exception as e:
        return jsonify({'connected': False, 'error': str(e)}), 500


@app.route('/api/sms')
def api_list_sms():
    try:
        # 从模组读取收到的短信
        recv_msgs = modem.list_sms("ALL")
        for m in recv_msgs:
            m['direction'] = 'received'
            m['id'] = f"recv_{m['index']}"

        # 从本地存储读取已发送短信
        sent_msgs = sent_store.get_all()

        # 合并并返回
        all_msgs = recv_msgs + sent_msgs
        return jsonify({'ok': True, 'messages': all_msgs})
    except RuntimeError as e:
        # 模组断连时仍返回已发送短信
        sent_msgs = sent_store.get_all()
        if sent_msgs:
            return jsonify({'ok': True, 'messages': sent_msgs})
        return jsonify({'ok': False, 'error': str(e)}), 503
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/sms/send', methods=['POST'])
def api_send_sms():
    data = request.get_json(force=True)
    phone = data.get('phone', '').strip()
    message = data.get('message', '').strip()
    if not phone or not message:
        return jsonify({'ok': False, 'error': '手机号和短信内容不能为空'}), 400
    try:
        success, detail = modem.send_sms(phone, message)
        if success:
            # 保存到本地发件箱
            sent_msg = sent_store.add(phone, message, detail)
            return jsonify({'ok': True, 'detail': detail, 'message': sent_msg})
        return jsonify({'ok': False, 'detail': detail})
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 503
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/sms/<msg_id>', methods=['DELETE'])
def api_delete_sms(msg_id):
    try:
        # 判断是收到的还是发送的短信
        if msg_id.startswith('sent_'):
            ok = sent_store.delete(msg_id)
            return jsonify({'ok': ok})
        elif msg_id.startswith('recv_'):
            # 提取模组索引
            index = msg_id[5:]
            ok = modem.delete_sms(index)
            return jsonify({'ok': ok})
        else:
            # 兼容旧格式：纯数字索引
            ok = modem.delete_sms(msg_id)
            return jsonify({'ok': ok})
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 503
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/sms', methods=['DELETE'])
def api_delete_all():
    try:
        # 同时清空模组和本地存储
        modem.delete_all_sms()
        sent_store.delete_all()
        return jsonify({'ok': True})
    except RuntimeError as e:
        # 模组断连时仍可清空本地
        sent_store.delete_all()
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/speed')
def api_speed():
    try:
        return jsonify(speed_monitor.get_speed())
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/data-usage')
def api_data_usage():
    try:
        return jsonify(data_usage.get_summary())
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/data-usage/reset', methods=['POST'])
def api_data_usage_reset():
    try:
        return jsonify(data_usage.reset())
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ─── Call API ────────────────────────────────────────────────────────
@app.route('/api/call/status')
def api_call_status():
    return jsonify({'ok': True, 'call': call_manager.get_status()})


@app.route('/api/call/dial', methods=['POST'])
def api_call_dial():
    data = request.get_json(force=True)
    number = normalize_phone(data.get('number', '').strip())
    if not CALL_NUMBER_PATTERN.fullmatch(number):
        return jsonify({'ok': False, 'error': '请输入有效的电话号码'}), 400
    try:
        ok = call_manager.dial(number)
        payload = {'ok': ok, 'call': call_manager.get_status()}
        if not ok:
            payload['error'] = '模组未接受拨号指令'
        return jsonify(payload), 200 if ok else 502
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e), 'call': call_manager.get_status()}), 409
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 503
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/call/answer', methods=['POST'])
def api_call_answer():
    try:
        ok = call_manager.answer()
        return jsonify({'ok': ok})
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 503
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/call/hangup', methods=['POST'])
def api_call_hangup():
    try:
        ok = call_manager.hangup()
        payload = {'ok': ok, 'call': call_manager.get_status()}
        if not ok:
            payload['error'] = '模组未确认挂断，请重试'
        return jsonify(payload), 200 if ok else 502
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 503
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/call/dtmf', methods=['POST'])
def api_call_dtmf():
    data = request.get_json(force=True)
    digit = data.get('digit', '').strip()
    if digit not in '0123456789*#' or len(digit) != 1:
        return jsonify({'ok': False, 'error': '无效的按键'}), 400
    try:
        ok = call_manager.send_dtmf(digit)
        return jsonify({'ok': ok})
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 503
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/call/reset', methods=['POST'])
def api_call_reset():
    """强制重置通话状态。"""
    try:
        call_manager.force_reset()
        return jsonify({'ok': True, 'call': call_manager.get_status()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ─── Call History API ────────────────────────────────────────────────

@app.route('/api/call/history')
def api_call_history():
    try:
        records = call_store.get_all()
        return jsonify({'ok': True, 'records': records})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/call/history/<record_id>', methods=['DELETE'])
def api_delete_call(record_id):
    try:
        ok = call_store.delete(record_id)
        return jsonify({'ok': ok})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/call/history', methods=['DELETE'])
def api_delete_all_calls():
    try:
        ok = call_store.delete_all()
        return jsonify({'ok': ok})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


if __name__ == '__main__':
    print("EG25-G SMS + Call Web Server")
    print("Starting on http://localhost:8080 ...")
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True)

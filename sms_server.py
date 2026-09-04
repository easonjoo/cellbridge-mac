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

    @staticmethod
    def _ucs2_encode(text):
        """文本 → UCS2 hex 字符串（用于 AT+CSCS="UCS2" 下的号码/内容）。"""
        return text.encode('utf-16-be').hex().upper()

    @staticmethod
    def _ucs2_decode(value):
        """UCS2 hex 字符串 → 文本；非 hex 内容原样返回（兼容纯英文号码）。"""
        v = value.strip().strip('"')
        if not v:
            return v
        try:
            if len(v) % 4 == 0 and all(c in '0123456789abcdefABCDEF' for c in v):
                return bytes.fromhex(v).decode('utf-16-be')
        except (ValueError, UnicodeDecodeError):
            pass
        return v

    def send_sms(self, phone, message):
        """发送短信（UCS2 编码，支持中英文）。"""
        with self._lock:
            if not self._ensure():
                raise RuntimeError("EG25-G 模块未连接")
            try:
                # 设置文本模式 + UCS2 字符集
                self.dev.write(EP_BULK_OUT, b'AT+CMGF=1\r', timeout=5000)
                self._drain(timeout=2000)
                self.dev.write(EP_BULK_OUT, b'AT+CSCS="UCS2"\r', timeout=5000)
                self._drain(timeout=2000)

                # 发送 CMGS 命令（号码用 UCS2 hex）
                phone_ucs2 = self._ucs2_encode(phone)
                cmd = f'AT+CMGS="{phone_ucs2}"\r'.encode('ascii')
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

                # 发送消息内容（UCS2 hex）+ Ctrl+Z
                msg_data = self._ucs2_encode(message).encode('ascii') + b'\x1a'
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
        """列出短信（UCS2 编码，支持中文）。"""
        # 先设置文本模式和字符集
        self.send_at('AT+CMGF=1')
        self.send_at('AT+CSCS="UCS2"')
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
                    parts['content'] = self._ucs2_decode(content)
                    parts['sender'] = self._ucs2_decode(parts.get('sender', ''))
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
        """读取指定索引的短信（UCS2 编码，支持中文）。"""
        self.send_at('AT+CMGF=1')
        self.send_at('AT+CSCS="UCS2"')
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
                    'sender': self._ucs2_decode(parts[1]) if len(parts) > 1 else '',
                    'timestamp': parts[3].strip().strip('"') if len(parts) > 3 else '',
                    'content': self._ucs2_decode(content),
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
            # CSCA 受当前字符集影响，切回 GSM 保证可读
            self.send_at('AT+CSCS="GSM"')
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


# ─── Module Traffic Monitor (基于模块 AT+QGDCNT 真实蜂窝流量) ─────────
class ModuleTrafficMonitor:
    """通过模块内部 AT+QGDCNT 计数器统计真实 4G 流量。

    旧方案用 psutil 读 Mac 网卡计数——模块的 ECM 网卡只承载 AT/管理流量，
    统计值恒为 0，毫无意义。QGDCNT 是基带层面的真实蜂窝收发字节，
    与系统路由无关。
    ⚠ 本固件(QDC507GLEFM21)实测 QGDCNT 返回「字节」而非官方文档标注的 KB
    （若按 KB 解释，寿命累计会达数百 TB，不可能；按字节解释则与实际情况吻合），
    因此读回后统一除以 1024 换算为 KB。
    模块重启后计数器清零，靠 _last 采样对比自动衔接。
    按天/按月的用量为「本应用开始跟踪以来」的增量，持久化到 JSON。
    """

    def __init__(self, filepath=None):
        self.filepath = filepath or os.path.join(BASE_DIR, 'data_usage.json')
        self._lock = threading.Lock()
        self._last = None          # (rx_kb, tx_kb, ts)
        self._history = []         # 最近 60 个采样点 {t, down, up} KB/s
        self._today = {'date': time.strftime('%Y-%m-%d'), 'recv': 0.0, 'sent': 0.0}
        self._month = {'month': time.strftime('%Y-%m'), 'recv': 0.0, 'sent': 0.0}
        self._last_save = 0.0
        self._last_resp = {'ok': False, 'error': '尚未采样'}
        self._load()

    # ---- 持久化 ----
    def _load(self):
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                d = json.load(f)
            if d.get('today', {}).get('date') == self._today['date']:
                self._today = d['today']
            if d.get('month', {}).get('month') == self._month['month']:
                self._month = d['month']
        except Exception:
            pass

    def _save(self):
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump({'today': self._today, 'month': self._month}, f)
        except Exception:
            pass

    # ---- 采样 ----
    def _read_qgdcnt(self):
        """返回 (rx_bytes, tx_bytes)，失败抛异常。固件返回的是字节。"""
        resp = modem.send_at('AT+QGDCNT?', timeout=6000)
        m = re.search(r'\+QGDCNT:\s*(\d+)\s*,\s*(\d+)', resp)
        if not m:
            raise RuntimeError(f'QGDCNT 响应异常: {resp[:80]}')
        return int(m.group(1)), int(m.group(2))

    def tick(self):
        """采样一次，更新速率/日/月统计。"""
        with self._lock:
            try:
                if not modem.is_connected():
                    self._last = None
                    return
                rxb, txb = self._read_qgdcnt()
                rx, tx = rxb / 1024.0, txb / 1024.0   # 字节 → KB
                now = time.time()
                down = up = 0.0
                if self._last is not None:
                    last_rx, last_tx, last_ts = self._last
                    dt = now - last_ts
                    if dt > 0 and rx >= last_rx and tx >= last_tx:
                        down = (rx - last_rx) / dt
                        up = (tx - last_tx) / dt
                        # 合理性钳制：单次采样不可能超过 200 Mbps(25600 KB/s)。
                        # 模块初始化/重启瞬间可能返回跳变值，直接丢弃本次增量。
                        if down > 25600 or up > 25600:
                            down = up = 0.0
                        else:
                            # 计入日/月增量
                            today = time.strftime('%Y-%m-%d')
                            month = time.strftime('%Y-%m')
                            if self._today['date'] != today:
                                self._today = {'date': today, 'recv': 0.0, 'sent': 0.0}
                            if self._month['month'] != month:
                                self._month = {'month': month, 'recv': 0.0, 'sent': 0.0}
                            self._today['recv'] += rx - last_rx
                            self._today['sent'] += tx - last_tx
                            self._month['recv'] += rx - last_rx
                            self._month['sent'] += tx - last_tx
                        self._history.append({'t': now, 'down': round(down, 1), 'up': round(up, 1)})
                        if len(self._history) > 60:
                            self._history.pop(0)
                self._last = (rx, tx, now)
                self._last_resp = {
                    'ok': True,
                    'source': 'qgdcnt',
                    'down': round(down, 1),   # KB/s
                    'up': round(up, 1),
                    'rx_total_kb': rx,        # 模块生命周期累计
                    'tx_total_kb': tx,
                    'today': {'recv_kb': round(self._today['recv'], 1), 'sent_kb': round(self._today['sent'], 1)},
                    'month': {'recv_kb': round(self._month['recv'], 1), 'sent_kb': round(self._month['sent'], 1)},
                    'history': list(self._history[-60:]),
                    'timestamp': now,
                }
                if now - self._last_save > 30:
                    self._save()
                    self._last_save = now
            except Exception as e:
                self._last_resp = {'ok': False, 'error': str(e), 'source': 'qgdcnt'}

    def get_speed(self):
        return dict(self._last_resp)

    def get_summary(self):
        return dict(self._last_resp)

    def reset(self):
        """清零模块计数器 + 本应用日/月统计。"""
        with self._lock:
            try:
                modem.send_at('AT+QGDCNT=0,0', timeout=6000)
            except Exception:
                pass
            self._last = None
            self._history = []
            self._today = {'date': time.strftime('%Y-%m-%d'), 'recv': 0.0, 'sent': 0.0}
            self._month = {'month': time.strftime('%Y-%m'), 'recv': 0.0, 'sent': 0.0}
            self._save()
            return {'ok': True}



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
        self._voice_prepared = False   # 本通电话是否已触发模块侧语音路由

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
                    self._ensure_voice_for_call()
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
                self._teardown_voice()

    # ---- 模块侧语音路由（voice_runtime）通话期钩子 ----
    def _ensure_voice_for_call(self):
        """通话进入 active 时后台准备模块侧语音路由（幂等）。"""
        if self._voice_prepared:
            return
        self._voice_prepared = True

        def _prep():
            try:
                voice_runtime.ensure_voice_route()
            except Exception as e:
                # 失败则复位标记，下次通话状态变化时重试
                self._voice_prepared = False
                print(f'语音路由准备失败: {e}', file=sys.stderr)
        threading.Thread(target=_prep, daemon=True).start()

    def _teardown_voice(self):
        """通话结束后拆除语音路由。"""
        if not self._voice_prepared:
            return
        self._voice_prepared = False

        def _stop():
            try:
                voice_runtime.stop_voice_route()
            except Exception as e:
                print(f'语音路由停止失败: {e}', file=sys.stderr)
        threading.Thread(target=_stop, daemon=True).start()

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
            self._ensure_voice_for_call()  # 拨号同时后台准备语音路由
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
            if ok:
                self._init_audio()
            return ok

    def _init_audio(self):
        """通话建立时初始化模组音频：音量、解除静音、麦克风增益（读设置）。"""
        try:
            vol = settings.get_all().get('call_volume', 5)
        except Exception:
            vol = 5
        try:
            mic = settings.get_all().get('mic_gain', 12)
        except Exception:
            mic = 12
        for cmd in (f'AT+CLVL={vol}', 'AT+QMUTE=0', f'AT+QMIC=1,{mic}'):
            try:
                self.modem.send_at(cmd, timeout=2000)
            except Exception:
                pass

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
                self._init_audio()
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
traffic = ModuleTrafficMonitor()
def _persistent_data_path(filename):
    """数据文件（设置/发件箱/通话记录）持久化到用户目录，避免 App 重建时丢失。"""
    base = os.path.expanduser('~/Library/Application Support/DJiPhoneKit')
    try:
        os.makedirs(base, exist_ok=True)
    except Exception:
        return os.path.join(BASE_DIR, filename)
    new_path = os.path.join(base, filename)
    legacy = os.path.join(BASE_DIR, filename)
    if not os.path.exists(new_path) and os.path.exists(legacy):
        try:
            import shutil
            shutil.copy2(legacy, new_path)
        except Exception:
            pass
    return new_path


sent_store = SentMessageStore(_persistent_data_path('sent_sms.json'))
call_store = CallRecordStore(_persistent_data_path('call_history.json'))
call_manager = CallManager(modem, call_store=call_store)
if os.environ.get('SMS_HUB_DISABLE_BACKGROUND') != '1':
    call_manager.start()


def _data_usage_loop():
    """后台线程，每 2 秒读一次模块 QGDCNT 计数器。"""
    time.sleep(4)  # 等模块连接就绪
    while True:
        try:
            traffic.tick()
        except Exception:
            pass
        time.sleep(2)


_data_usage_thread = threading.Thread(target=_data_usage_loop, daemon=True)
_data_usage_thread.start()


@app.route('/')
def index():
    return send_file(os.path.join(BASE_DIR, 'index.html'))


@app.route('/m')
@app.route('/m/')
def mobile():
    return send_file(os.path.join(BASE_DIR, 'mobile.html'))


@app.route('/manifest.webmanifest')
def webmanifest():
    return send_file(os.path.join(BASE_DIR, 'manifest.webmanifest'),
                     mimetype='application/manifest+json')


@app.route('/app-icon.png')
def app_icon_png():
    return send_file(os.path.join(BASE_DIR, 'assets', 'icon.png'), mimetype='image/png')


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
        return jsonify(traffic.get_speed())
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/data-usage')
def api_data_usage():
    try:
        return jsonify(traffic.get_summary())
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/data-usage/reset', methods=['POST'])
def api_data_usage_reset():
    try:
        return jsonify(traffic.reset())
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ─── Settings Store (应用设置持久化) ─────────────────────────────────
LAUNCH_AGENT_LABEL = 'local.idoer.djiphone'
LAUNCH_AGENT_LEGACY_LABELS = ('local.idoer.sms-hub',)
LAUNCH_AGENT_PATH = os.path.expanduser(f'~/Library/LaunchAgents/{LAUNCH_AGENT_LABEL}.plist')
APP_BUNDLE_PATH = os.path.expanduser('~/Applications/DJiPhone Kit.app')
APP_BUNDLE_LEGACY_PATHS = (os.path.expanduser('~/Applications/DjiPhone.app'),)

DEFAULT_SETTINGS = {
    'autostart': True,        # 开机自启（LaunchAgent）
    'lan_pin': '',            # 局域网访问 PIN（空=不启用鉴权）
    'call_volume': 5,         # 通话听筒音量 0-5
    'mic_gain': 12,           # 麦克风增益
    'notify_new_sms': True,   # 新短信通知（前端轮询提示）
    'sms_forward': {          # 短信转发（参考 CellDock）
        'enabled': False,
        'bark_url': '',        # 例: https://api.day.app/你的Key
        'feishu_webhook': '',  # 飞书自定义机器人 webhook
        'feishu_secret': '',   # 飞书加签密钥（可选）
        'dingtalk_webhook': '',  # 钉钉自定义机器人 webhook
        'dingtalk_secret': '',   # 钉钉加签密钥（可选）
    },
}


class SettingsStore:
    def __init__(self, filepath):
        self.filepath = filepath
        self._lock = threading.Lock()
        self._data = dict(DEFAULT_SETTINGS)
        self._load()

    def _load(self):
        try:
            with open(self.filepath, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    for k in DEFAULT_SETTINGS:
                        if k not in saved:
                            continue
                        # 嵌套设置（如 sms_forward）做字段级合并，保留新增字段的默认值
                        if isinstance(DEFAULT_SETTINGS[k], dict) and isinstance(saved[k], dict):
                            merged = dict(DEFAULT_SETTINGS[k])
                            merged.update({sk: sv for sk, sv in saved[k].items() if sk in DEFAULT_SETTINGS[k]})
                            self._data[k] = merged
                        else:
                            self._data[k] = saved[k]
        except Exception:
            pass

    def _save(self):
        try:
            with open(self.filepath, 'w', encoding='utf-8') as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def get_all(self):
        with self._lock:
            return dict(self._data)

    def set_many(self, updates):
        with self._lock:
            for k, v in updates.items():
                if k not in DEFAULT_SETTINGS:
                    continue
                if isinstance(DEFAULT_SETTINGS[k], dict) and isinstance(v, dict):
                    merged = dict(self._data[k])
                    merged.update({sk: sv for sk, sv in v.items() if sk in DEFAULT_SETTINGS[k]})
                    self._data[k] = merged
                else:
                    self._data[k] = v
            self._save()
            return dict(self._data)


def _write_launch_agent(enabled):
    """启用/禁用开机自启（LaunchAgent plist）。同时清理历史遗留的旧标签。"""
    try:
        for legacy in LAUNCH_AGENT_LEGACY_LABELS:
            legacy_path = os.path.expanduser(f'~/Library/LaunchAgents/{legacy}.plist')
            try:
                os.unlink(legacy_path)
            except FileNotFoundError:
                pass
        for legacy_app in APP_BUNDLE_LEGACY_PATHS:
            if os.path.isdir(legacy_app):
                import shutil
                try:
                    shutil.rmtree(legacy_app)
                except Exception:
                    pass
        if enabled:
            # 优先指向已安装的 App，否则用仓库里的 app.py
            if os.path.isdir(APP_BUNDLE_PATH):
                exe = os.path.join(APP_BUNDLE_PATH, 'Contents/MacOS/DJiPhone Kit')
            else:
                exe = os.path.join(BASE_DIR, 'app.py')
            python_bin = os.path.expanduser('~/.workbuddy/binaries/python/envs/default/bin/python3')
            if exe.endswith('.py') and os.path.exists(python_bin):
                program_args = [python_bin, exe]
            else:
                program_args = [exe]
            plist = f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>{''.join(f'<string>{a}</string>' for a in program_args)}</array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><false/>
</dict>
</plist>
'''
            os.makedirs(os.path.dirname(LAUNCH_AGENT_PATH), exist_ok=True)
            with open(LAUNCH_AGENT_PATH, 'w', encoding='utf-8') as f:
                f.write(plist)
        else:
            try:
                os.unlink(LAUNCH_AGENT_PATH)
            except FileNotFoundError:
                pass
        return True
    except Exception as e:
        print(f'LaunchAgent 操作失败: {e}', file=sys.stderr)
        return False


settings = SettingsStore(_persistent_data_path('settings.json'))

# ─── LAN 局域网访问 ──────────────────────────────────────────────────
import socket
import base64

def _get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(('223.5.5.5', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return '127.0.0.1'

# 局域网 PIN 鉴权：页面/静态资源免 token，API 需 token（query/header/cookie）
LOCAL_ADDRS = {'127.0.0.1', '::1', 'localhost'}
PUBLIC_PATHS = ('/', '/m', '/m/', '/index.html', '/mobile.html',
                '/manifest.webmanifest', '/app-icon.png', '/favicon.ico',
                '/api/lan', '/api/pin-login')


@app.before_request
def _lan_auth():
    if request.remote_addr in LOCAL_ADDRS:
        return None
    if request.path in PUBLIC_PATHS:
        return None
    pin = settings.get_all().get('lan_pin', '')
    if not pin:
        return None
    token = (request.args.get('token') or request.headers.get('X-Auth-Token')
             or request.cookies.get('dj_token') or '')
    if token == pin:
        return None
    if request.path.startswith('/api/'):
        return jsonify({'ok': False, 'error': '需要访问 PIN', 'need_pin': True}), 401
    return jsonify({'ok': False, 'error': '需要访问 PIN', 'need_pin': True}), 401


@app.route('/api/pin-login', methods=['POST'])
def api_pin_login():
    data = request.get_json(force=True) or {}
    pin = (data.get('pin') or '').strip()
    real = settings.get_all().get('lan_pin', '')
    if not real:
        return jsonify({'ok': True, 'need_pin': False})
    if pin == real:
        resp = jsonify({'ok': True})
        resp.set_cookie('dj_token', pin, max_age=30 * 86400, httponly=True,
                        samesite='Lax')
        return resp
    return jsonify({'ok': False, 'error': 'PIN 不正确'}), 401


@app.route('/api/lan')
def api_lan():
    ip = _get_lan_ip()
    pin = settings.get_all().get('lan_pin', '')
    url = f'http://{ip}:8080' + (f'/?token={pin}' if pin else '')
    qr = ''
    try:
        import qrcode
        import io
        img = qrcode.make(url)
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        qr = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        pass
    return jsonify({'ok': True, 'ip': ip, 'port': 8080, 'url': url, 'qr': qr, 'pin_enabled': bool(pin)})


# ─── Contacts 通讯录 ─────────────────────────────────────────────────
_contacts_cache = {'ts': 0.0, 'data': [], 'error': None}
CONTACTS_TTL = 300  # 秒


def _fetch_contacts_macos():
    """通过 pyobjc Contacts 框架读取 Mac 通讯录。首次调用触发系统授权弹窗。"""
    import Contacts as C
    import threading as _th

    store = C.CNContactStore.alloc().init()
    ev = _th.Event()
    granted = {'v': False, 'err': None}

    def _access_done(g, error):
        granted['v'] = bool(g)
        if error is not None:
            try:
                granted['err'] = error.localizedDescription()
            except Exception:
                pass
        ev.set()

    store.requestAccessForEntityType_completionHandler_(C.CNEntityTypeContacts, _access_done)
    if not ev.wait(timeout=30):
        raise RuntimeError('通讯录授权超时，请在 系统设置→隐私与安全性→通讯录 中允许本应用')
    if not granted['v']:
        raise RuntimeError('通讯录访问被拒绝：请在 系统设置→隐私与安全性→通讯录 中允许本应用')

    keys = [C.CNContactGivenNameKey, C.CNContactFamilyNameKey,
            C.CNContactNicknameKey, C.CNContactPhoneNumbersKey,
            C.CNContactOrganizationNameKey]
    req = C.CNContactFetchRequest.alloc().initWithKeysToFetch_(keys)
    found = []

    def _each(contact, stop):
        name = ' '.join(x for x in (contact.familyName(), contact.givenName()) if x).strip()
        if not name:
            name = contact.nickname() or contact.organizationName() or '无名联系人'
        phones = []
        for lv in contact.phoneNumbers():
            try:
                num = lv.value().stringValue()
                if num:
                    phones.append(num.replace(' ', '').replace('-', ''))
            except Exception:
                pass
        if phones:
            found.append({'name': name, 'phones': phones})
        return True

    ok = store.enumerateContactsWithFetchRequest_error_usingBlock_(req, None, _each)
    if not ok:
        raise RuntimeError('通讯录读取失败（可能未授权）')
    found.sort(key=lambda c: c['name'])
    return found


@app.route('/api/contacts')
def api_contacts():
    query = (request.args.get('query') or '').strip().lower()
    now = time.time()
    if _contacts_cache['data'] or _contacts_cache['error']:
        if now - _contacts_cache['ts'] > CONTACTS_TTL:
            _contacts_cache.update({'ts': 0, 'data': [], 'error': None})
    if not _contacts_cache['data'] and not _contacts_cache['error']:
        try:
            data = _fetch_contacts_macos()
            _contacts_cache.update({'ts': now, 'data': data, 'error': None})
        except ImportError:
            err = '服务器缺少 Contacts 支持'
            _contacts_cache.update({'ts': now, 'data': [], 'error': err})
            return jsonify({'ok': False, 'error': err}), 501
        except Exception as e:
            err = str(e)
            _contacts_cache.update({'ts': now, 'data': [], 'error': err})
            return jsonify({'ok': False, 'error': err}), 403

    if _contacts_cache['error']:
        return jsonify({'ok': False, 'error': _contacts_cache['error']}), 403

    data = _contacts_cache['data']
    if query:
        def _match(c):
            if query in c['name'].lower():
                return True
            return any(query in p.lower() for p in c['phones'])
        data = [c for c in data if _match(c)]
    return jsonify({'ok': True, 'contacts': data[:200], 'total': len(data)})


# ─── Call 音频控制 ───────────────────────────────────────────────────
@app.route('/api/call/volume', methods=['POST'])
def api_call_volume():
    data = request.get_json(force=True)
    try:
        level = max(0, min(5, int(data.get('level', 5))))
    except (TypeError, ValueError):
        return jsonify({'ok': False, 'error': '音量需为 0-5'}), 400
    mic = data.get('mic_gain')
    updates = {'call_volume': level}
    if mic is not None:
        try:
            updates['mic_gain'] = max(0, min(15, int(mic)))
        except (TypeError, ValueError):
            pass
    settings.set_many(updates)
    results = {}
    try:
        results['clvl'] = modem.send_at(f'AT+CLVL={level}', timeout=3000)
        results['qmic'] = modem.send_at(
            f"AT+QMIC=1,{settings.get_all()['mic_gain']}", timeout=3000)
        results['mute'] = modem.send_at('AT+QMUTE=0', timeout=3000)
        return jsonify({'ok': True, 'results': results})
    except RuntimeError as e:
        return jsonify({'ok': False, 'error': str(e)}), 503
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# ─── Settings API ────────────────────────────────────────────────────
@app.route('/api/settings', methods=['GET'])
def api_settings_get():
    data = settings.get_all()
    data['autostart_installed'] = os.path.exists(LAUNCH_AGENT_PATH)
    return jsonify({'ok': True, 'settings': data})


@app.route('/api/settings', methods=['POST'])
def api_settings_set():
    data = request.get_json(force=True) or {}
    updates = {}
    for k in DEFAULT_SETTINGS:
        if k in data:
            updates[k] = data[k]
    if not updates:
        return jsonify({'ok': False, 'error': '没有可更新的设置项'}), 400
    saved = settings.set_many(updates)
    if 'autostart' in updates:
        saved['autostart_installed'] = _write_launch_agent(bool(updates['autostart']))
    else:
        saved['autostart_installed'] = os.path.exists(LAUNCH_AGENT_PATH)
    return jsonify({'ok': True, 'settings': saved})


# ─── Voice 语音诊断/修复（参考 dji-4g-connect 的 IMS+UAC 配方）────────
def _parse_usbcfg_flags(resp):
    """从 AT+QCFG="usbcfg" 响应解析 9 个字段（vid,pid + 7 个开关位）。"""
    import re
    m = re.search(r'\+QCFG:\s*"usbcfg",\s*(0x[0-9A-Fa-f]+|\d+),(0x[0-9A-Fa-f]+|\d+),([01]),([01]),([01]),([01]),([01]),([01]),([01])', resp)
    if not m:
        return None
    return [m.group(i).strip() for i in range(1, 10)]


def _voice_diag():
    """采集模块语音链路配置，返回诊断结果。"""
    import re
    diag = {}
    try:
        diag['ims'] = modem.send_at('AT+QCFG="ims"', timeout=5000)
    except Exception as e:
        diag['ims'] = f'<ERROR {e.__class__.__name__}>'
    try:
        diag['usbcfg'] = modem.send_at('AT+QCFG="usbcfg"', timeout=5000)
    except Exception as e:
        diag['usbcfg'] = f'<ERROR {e.__class__.__name__}>'
    try:
        diag['qpcmv'] = modem.send_at('AT+QPCMV?', timeout=3000)
    except Exception as e:
        diag['qpcmv'] = f'<ERROR {e.__class__.__name__}>'
    try:
        diag['ceer'] = modem.send_at('AT+CEER', timeout=3000)
    except Exception:
        diag['ceer'] = ''
    try:
        diag['cereg'] = modem.send_at('AT+CEREG?', timeout=3000)
    except Exception:
        diag['cereg'] = ''

    ims_ok = bool(re.search(r'\+QCFG:\s*"ims",\s*1(?:\s|,|$)', diag['ims']))
    flags = _parse_usbcfg_flags(diag['usbcfg'])
    # usbcfg 末两位: [音频接口, voice/audio 组合位]（dji-4g-connect 实测应均为 1）
    uac_ok = bool(flags) and flags[-2] == '1' and flags[-1] == '1'
    pcm_ok = ('+QPCMV' in diag['qpcmv']) and ('OK' in diag['qpcmv']) and ('ERROR' not in diag['qpcmv'])
    # VoLTE 注册: CEREG 带 [4]/[5]（含 IMS 注册指示位）或直接看网络
    volte_reg = bool(re.search(r'\+CEREG:\s*\d+,(\d+)[^,]*(?:,[^,]*){2},\d', diag['cereg']))
    return {
        'ims_enabled': ims_ok,
        'uac_ok': uac_ok,
        'usbcfg_flags': flags,
        'pcm_ok': pcm_ok,
        'raw': diag,
        # pcm_ok=False 时固件无法把通话媒体路由到 USB 音频（需模块侧运行时，见 PROCESS.md）
        'verdict': ('ready' if (ims_ok and uac_ok and pcm_ok)
                    else 'partial' if (ims_ok and uac_ok)
                    else 'needs_fix'),
        'detail': f'IMS {"开" if ims_ok else "关"} · USB音频 {"OK" if uac_ok else "缺位"} · PCM路由 {"OK" if pcm_ok else "不支持/未配置"}',
    }


@app.route('/api/voice/diag')
def api_voice_diag():
    try:
        if not modem.is_connected():
            return jsonify({'ok': False, 'error': '模块未连接'}), 503
        diag = _voice_diag()
        # 附带 USB 接口清单（诊断 ADB/音频接口是否存在）
        try:
            import usb.core
            dev = usb.core.find(idVendor=0x2CA3, idProduct=0x4006)
            ifaces = []
            if dev is not None:
                for intf in dev.get_active_configuration():
                    try:
                        ifaces.append({'num': intf.bInterfaceNumber,
                                       'class': intf.bInterfaceClass,
                                       'subclass': intf.bInterfaceSubClass,
                                       'proto': intf.bInterfaceProtocol})
                    except AttributeError:
                        pass
            diag['usb_interfaces'] = ifaces
        except Exception:
            pass
        return jsonify({'ok': True, 'diag': diag})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/at', methods=['POST'])
def api_at_debug():
    """AT 调试通道：直接向模块发送一条 AT 指令（仅本机/带 PIN 的局域网可访问）。"""
    data = request.get_json(force=True) or {}
    cmd = (data.get('command') or data.get('cmd') or '').strip()
    if not cmd:
        return jsonify({'ok': False, 'error': 'command 不能为空'}), 400
    if len(cmd) > 200 or '\n' in cmd or '\r' in cmd:
        return jsonify({'ok': False, 'error': '指令格式无效'}), 400
    try:
        if not modem.is_connected():
            return jsonify({'ok': False, 'error': '模块未连接'}), 503
        resp = modem.send_at(cmd, timeout=8000)
        return jsonify({'ok': True, 'response': resp.replace('\r', '').strip()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/gps')
def api_gps():
    """GPS 状态与定位。"""
    try:
        if not modem.is_connected():
            return jsonify({'ok': False, 'error': '模块未连接'}), 503
        state = modem.send_at('AT+QGPS?', timeout=4000)
        on = '+QGPS: 1' in state
        result = {'ok': True, 'on': on, 'lat': None, 'lon': None, 'fix': None,
                  'satellites': None, 'altitude': None, 'speed': None, 'time': None}
        if on:
            loc = modem.send_at('AT+QGPSLOC?', timeout=6000)
            if '+QGPSLOC:' in loc:
                try:
                    parts = loc.split('+QGPSLOC:')[1].strip().split(',')
                    # utc,lat,lon,hdop,alt,fix,cog,spk km,spk kn,date,nsat
                    result['time'] = parts[0].strip()
                    result['lat'] = float(parts[1])
                    result['lon'] = float(parts[2])
                    result['altitude'] = float(parts[4])
                    result['fix'] = int(parts[5])
                    result['speed'] = float(parts[7])
                    result['satellites'] = int(parts[10])
                except (ValueError, IndexError):
                    result['loc_error'] = '尚未定位成功（户外空旷处需 1-2 分钟）'
        return jsonify(result)
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/gps/power', methods=['POST'])
def api_gps_power():
    """GPS 电源开关。"""
    data = request.get_json(force=True) or {}
    on = bool(data.get('on'))
    try:
        if not modem.is_connected():
            return jsonify({'ok': False, 'error': '模块未连接'}), 503
        resp = modem.send_at(f'AT+QGPS={"1" if on else "0"}', timeout=8000)
        ok = 'OK' in resp
        return jsonify({'ok': ok, 'on': on, 'response': resp.strip()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/voice/apply', methods=['POST'])
def api_voice_apply():
    """一键应用语音配置：usbcfg 末两位=1,1 + 开启 IMS + 保存 + 重启模组。"""
    steps = []
    try:
        if not modem.is_connected():
            return jsonify({'ok': False, 'error': '模块未连接'}), 503
        resp = modem.send_at('AT+QCFG="usbcfg"', timeout=5000)
        flags = _parse_usbcfg_flags(resp)
        if not flags:
            return jsonify({'ok': False, 'error': f'无法解析 usbcfg: {resp}'}), 500
        target = flags[:7] + ['1', '1']
        cmd = 'AT+QCFG="usbcfg",' + ','.join(target)
        steps.append({'cmd': cmd, 'resp': modem.send_at(cmd, timeout=5000)})
        steps.append({'cmd': 'AT+QCFG="ims",1', 'resp': modem.send_at('AT+QCFG="ims",1', timeout=5000)})
        steps.append({'cmd': 'AT&W', 'resp': modem.send_at('AT&W', timeout=5000)})
        try:
            steps.append({'cmd': 'AT+CFUN=1,1', 'resp': modem.send_at('AT+CFUN=1,1', timeout=8000)})
        except Exception as e:
            steps.append({'cmd': 'AT+CFUN=1,1', 'resp': f'模组重启中（{e.__class__.__name__}，属预期）'})
        return jsonify({'ok': True, 'steps': steps, 'note': '模组正在重启，约 30-60 秒后重新连接，届时请再次运行诊断'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), 'steps': steps}), 500


# ─── SMS Forwarder 短信转发（参考 CellDock）──────────────────────────
import hashlib
import hmac as _hmac
import time as _time
import urllib.request as _urlreq

_forward_state = {
    'seen': {},      # id -> content hash，启动后首 seen 只记录不转发
    'primed': False,
}


def _sign_secret(secret, ts):
    """飞书/钉钉加签。"""
    string_to_sign = f'{ts}\n{secret}'
    import base64
    hmac_code = _hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
    return base64.b64encode(hmac_code).decode()


def _http_post_json(url, payload, timeout=10):
    req = _urlreq.Request(url, data=json.dumps(payload).encode('utf-8'),
                          headers={'Content-Type': 'application/json'})
    with _urlreq.urlopen(req, timeout=timeout) as r:
        body = r.read().decode('utf-8', errors='replace')
        return r.status, body


def _forward_bark(cfg, sender, content):
    base = (cfg.get('bark_url') or '').rstrip('/')
    if not base:
        return False, '未配置 bark_url'
    from urllib.parse import quote
    title = quote(f'短信来自 {sender}')
    body = quote(content[:500])
    url = f'{base}/{title}/{body}'
    with _urlreq.urlopen(url, timeout=10) as r:
        return r.status == 200, f'HTTP {r.status}'


def _forward_feishu(cfg, sender, content):
    url = cfg.get('feishu_webhook') or ''
    if not url:
        return False, '未配置 feishu_webhook'
    secret = cfg.get('feishu_secret') or ''
    payload = {'msg_type': 'text', 'content': {'text': f'📩 {sender}\n{content}'}}
    if secret:
        payload['timestamp'] = str(int(_time.time()))
        payload['sign'] = _sign_secret(secret, int(_time.time()))
    status, body = _http_post_json(url, payload)
    ok = status == 200 and ('StatusCode' not in body or '0' in body[:60])
    return ok, f'HTTP {status}: {body[:80]}'


def _forward_dingtalk(cfg, sender, content):
    url = cfg.get('dingtalk_webhook') or ''
    if not url:
        return False, '未配置 dingtalk_webhook'
    secret = cfg.get('dingtalk_secret') or ''
    if secret:
        from urllib.parse import quote
        ts = str(round(_time.time() * 1000))
        sign = _sign_secret(secret, int(ts))
        url += f'&timestamp={ts}&sign={quote(sign)}'
    payload = {'msgtype': 'text', 'text': {'content': f'📩 {sender}\n{content}'}}
    status, body = _http_post_json(url, payload)
    ok = status == 200 and '"errcode":0' in body.replace(' ', '')
    return ok, f'HTTP {status}: {body[:80]}'


def _forward_one_sms(sender, content, timestamp=''):
    cfg = settings.get_all().get('sms_forward') or {}
    results = {}
    if not cfg.get('enabled'):
        return {'skipped': '转发未启用'}
    for name, fn in (('bark', _forward_bark), ('feishu', _forward_feishu), ('dingtalk', _forward_dingtalk)):
        try:
            ok, detail = fn(cfg, sender, content)
            results[name] = {'ok': ok, 'detail': detail}
        except Exception as e:
            results[name] = {'ok': False, 'detail': f'{e.__class__.__name__}: {e}'}
    return results


@app.route('/api/sms-forward/test', methods=['POST'])
def api_sms_forward_test():
    try:
        results = _forward_one_sms('10086', '这是一条 DJiPhone Kit 短信转发测试消息')
        return jsonify({'ok': True, 'results': results})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


def _sms_forward_loop():
    """后台轮询新短信并转发。首轮只记录不转发，避免刷历史短信。"""
    time.sleep(6)  # 避开启动期其他线程的 USB 并发枚举
    while True:
        try:
            cfg = settings.get_all().get('sms_forward') or {}
            msgs = modem.list_sms('ALL') if (cfg.get('enabled') or not _forward_state['primed']) else []
            current = {}
            for m in msgs:
                stat = (m.get('status') or '')
                if not stat.startswith('REC'):
                    continue  # 只关心收到的短信
                mid = f"recv_{m.get('index')}"
                h = hashlib.sha1(f"{m.get('sender')}|{m.get('timestamp')}|{m.get('content')}".encode()).hexdigest()
                current[mid] = h
                old = _forward_state['seen'].get(mid)
                if _forward_state['primed'] and cfg.get('enabled') and old != h:
                    _forward_one_sms(m.get('sender', ''), m.get('content', ''), m.get('timestamp', ''))
            _forward_state['seen'] = current
            _forward_state['primed'] = True
        except Exception:
            pass
        time.sleep(10)


forward_thread = threading.Thread(target=_sms_forward_loop, daemon=True)
forward_thread.start()


# ─── Voice Runtime 模块侧语音运行时 ──────────────────────────────────
import voice_runtime


@app.route('/api/voice/runtime')
def api_voice_runtime():
    return jsonify({'ok': True, **voice_runtime.voice_status()})


@app.route('/api/voice/provision', methods=['POST'])
def api_voice_provision():
    data = request.get_json(force=True) or {}
    if not data.get('confirm'):
        return jsonify({'ok': False, 'error': '需要确认后才会从上游获取模块侧语音运行时'}), 400
    try:
        voice_runtime.provision_runtime()
        return jsonify({'ok': True, **voice_runtime.voice_status()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), **voice_runtime.voice_status()}), 502


@app.route('/api/voice/start', methods=['POST'])
def api_voice_start():
    try:
        voice_runtime.ensure_voice_route()
        return jsonify({'ok': True, **voice_runtime.voice_status()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), **voice_runtime.voice_status()}), 502


@app.route('/api/voice/stop', methods=['POST'])
def api_voice_stop():
    try:
        voice_runtime.stop_voice_route()
        return jsonify({'ok': True, **voice_runtime.voice_status()})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e), **voice_runtime.voice_status()}), 502


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

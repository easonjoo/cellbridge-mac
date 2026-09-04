#!/usr/bin/env python3
"""探测 QDC507 语音/VoLTE 配置，排查无声音与 15-18s 自动挂断。"""
import sys
from sms_server import ModemManager

QUERIES = [
    ('AT+CEER',        '上次释放原因'),
    ('AT+QNWINFO',     '网络信息'),
    ('AT+CEREG?',      'EPS 注册状态'),
    ('AT+QCFG="ims"',  'IMS 配置'),
    ('AT+QMBNCFG="AutoSel"', 'MBN 自动选择'),
    ('AT+QMBNCFG="List"',    'MBN 列表'),
    ('AT+QDAI?',       '音频数字接口'),
    ('AT+QPCMV?',      'PCM 语音模式'),
    ('AT+QCFG="usbcfg"', 'USB 配置'),
    ('AT+CLVL?',       '音量等级'),
    ('AT+QMIC?',       '麦克风增益'),
    ('AT+CNUM',        '本机号码'),
    ('AT+QCSQN?',      '运营商名'),
]


def main():
    mm = ModemManager()
    mm._connect()
    if not mm.is_connected():
        print("ERROR: 模块未连接")
        sys.exit(1)
    for cmd, label in QUERIES:
        try:
            resp = mm.send_at(cmd, timeout=8000)
        except Exception as e:
            resp = f"<异常: {e}>"
        resp = resp.replace('\r', '').strip()
        print(f"[{label}] {cmd}\n{resp}\n{'-'*50}")
    mm._disconnect()


if __name__ == "__main__":
    main()

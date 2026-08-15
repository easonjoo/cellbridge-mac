#!/usr/bin/env python3
import os
import tempfile
import time
import unittest

os.environ['SMS_HUB_DISABLE_BACKGROUND'] = '1'

from sms_server import CallManager, CallRecordStore, ModemManager


class FakeModem:
    def __init__(self):
        self.commands = []
        self.responses = {}
        self.clcc_responses = []
        self.fail_commands = set()

    def send_at(self, command, timeout=5000, wait_ok=True):
        self.commands.append(command)
        if command in self.fail_commands:
            raise RuntimeError(f'{command} failed')
        if command == 'AT+CLCC' and self.clcc_responses:
            return self.clcc_responses.pop(0)
        return self.responses.get(command, 'OK')

    def _split_csv(self, value):
        return ModemManager._split_csv(self, value)


class CallManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.history_path = os.path.join(self.temp_dir.name, 'calls.json')
        self.store = CallRecordStore(self.history_path)
        self.modem = FakeModem()
        self.manager = CallManager(self.modem, self.store)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_outgoing_call_can_be_hung_up_and_recorded(self):
        self.assertTrue(self.manager.dial('13800138000'))
        self.assertEqual(self.manager.get_status()['state'], 'dialing')

        with self.manager._lock:
            self.manager._state = 'active'
            self.manager._call_start = time.time() - 3

        self.assertTrue(self.manager.hangup())
        self.assertEqual(self.manager.get_status()['state'], 'idle')
        self.assertIn('AT+CHUP', self.modem.commands)

        records = self.store.get_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['number'], '13800138000')
        self.assertEqual(records[0]['type'], 'outgoing')
        self.assertGreaterEqual(records[0]['duration'], 2)

        reloaded = CallRecordStore(self.history_path)
        self.assertEqual(reloaded.get_all()[0]['id'], records[0]['id'])

    def test_failed_hangup_keeps_call_active_and_does_not_record(self):
        self.assertTrue(self.manager.dial('10086'))
        self.modem.fail_commands.update({'AT+CHUP', 'ATH'})

        self.assertFalse(self.manager.hangup())
        self.assertEqual(self.manager.get_status()['state'], 'dialing')
        self.assertEqual(self.store.get_all(), [])

    def test_consecutive_missed_calls_are_both_recorded(self):
        self.modem.clcc_responses = [
            '+CLCC: 1,1,4,0,0,"13800138001",129\r\nOK',
            'OK',
            '+CLCC: 1,1,4,0,0,"13800138002",129\r\nOK',
            'OK',
        ]

        for _ in range(4):
            self.manager._check()

        records = self.store.get_all()
        self.assertEqual(len(records), 2)
        self.assertEqual({record['number'] for record in records}, {'13800138001', '13800138002'})
        self.assertTrue(all(record['type'] == 'missed' for record in records))

    def test_failed_dial_is_saved_as_an_outgoing_attempt(self):
        self.modem.responses['ATD10010;'] = 'ERROR'

        self.assertFalse(self.manager.dial('10010'))
        self.assertEqual(self.manager.get_status()['state'], 'idle')
        records = self.store.get_all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['number'], '10010')
        self.assertEqual(records[0]['type'], 'outgoing')
        self.assertEqual(records[0]['duration'], 0)


if __name__ == '__main__':
    unittest.main()

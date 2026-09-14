import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
import zipfile

import device
from server import Handler, ThreadingHTTPServer, Workbench, save_json


class PlipaTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.w = Workbench(Path(self.temp.name))
        self.w.current = {'id': 'test', 'serial': 'emulator-5554', 'package': device.PACKAGE,
                          'title': '테스트', 'environment': {'package': device.PACKAGE}}
        self.w.directory().mkdir()
        save_json(self.w.directory()/'session.json', self.w.current)
        self.nodes = [{'id': 0, 'text': '삭제', 'bounds': [0, 0, 100, 100],
                       'package': device.PACKAGE, 'enabled': True, 'clickable': True}]
        self.action = {'kind': 'tap', 'target': 0, 'text': '', 'message': '삭제 버튼'}

    def test_unsafe_actions_never_auto_run(self):
        self.assertFalse(device.auto_allowed(self.action))
        self.assertFalse(device.auto_allowed({'kind': 'text'}))
        self.assertFalse(device.auto_allowed({'kind': 'key'}))
        self.assertTrue(device.auto_allowed({'kind': 'observe'}))
        with patch('device.adb') as adb:
            for action in [{'kind':'shell','text':'rm'}, {'kind':'key','text':'POWER'},
                           {'kind':'tap','coords':[-1,0]}, {'kind':'tap','target':-1}]:
                with self.assertRaises(ValueError):
                    device.perform('emulator-5554', action, self.nodes if 'target' in action else None)
            adb.assert_not_called()

    def test_approval_cancel_and_stale_state(self):
        with patch('device.tree', return_value=self.nodes), patch('device.perform') as perform, patch.object(self.w, '_spawn'):
            self.w.pending = {'id': 'first', 'action': self.action, 'nodes': self.nodes}
            self.w.approve('first', False)
            perform.assert_not_called()
            with self.assertRaises(ValueError):
                self.w.approve('first', True)
            self.w.pending = {'id': 'second', 'action': self.action, 'nodes': []}
            with self.assertRaises(ValueError):
                self.w.approve('second', True)
            perform.assert_not_called()
            self.w.pending = {'id': 'third', 'action': self.action, 'nodes': self.nodes}
            self.w.approve('third', True)
            perform.assert_called_once()
            with self.assertRaises(ValueError):
                self.w.approve('third', True)

    def test_ai_loop_stops_before_mutation(self):
        with patch('device.tree', return_value=self.nodes), patch('ai.plan', return_value=self.action), patch('device.perform') as perform:
            self.w.goal = '삭제를 테스트'
            self.w.running = True
            self.w._loop()
            self.assertIsNotNone(self.w.pending)
            self.assertFalse(self.w.running)
            perform.assert_not_called()
            self.w.cancel()
            self.assertIsNone(self.w.pending)
            self.assertTrue(self.w.stop.is_set())

    def test_record_recovery_after_disconnect(self):
        self.w.event('note', '기존 관찰')
        with patch('device.tree', side_effect=ValueError('연결 끊김')), patch('device.logs', side_effect=ValueError('연결 끊김')):
            self.w.capture()
        restored = Workbench(Path(self.temp.name))
        restored.load('test')
        self.assertEqual(restored.events()[0]['message'], '기존 관찰')
        self.assertEqual(len(restored.events()[-1]['missing']), 2)
        with (restored.directory()/'timeline.jsonl').open('a') as out:
            out.write('{"interrupted":')
        restored.event('note', '복구 뒤 새 기록')
        self.assertEqual(restored.events()[-1]['message'], '복구 뒤 새 기록')

    def test_export_selection_and_linked_step(self):
        original = self.w.event('capture', '선택한 구간')
        self.w.event('note', '선택하지 않은 민감 메모')
        (self.w.directory()/'sample.png').write_bytes(b'example image')
        (self.w.directory()/'sample.txt').write_text('example log')
        attachment = self.w.event('evidence', '증거', step_id=original['id'], attachments=['sample.png','sample.txt'])
        data = {'ids':[attachment['id']], 'title':'문제','steps':'재현','expected':'기대','actual':'실제','logs':True}
        archive = zipfile.ZipFile(io.BytesIO(self.w.export(data)))
        self.assertIn('attachments/sample.txt', archive.namelist())
        self.assertNotIn('attachments/sample.png', archive.namelist())
        events = [json.loads(x) for x in archive.read('timeline.jsonl').splitlines()]
        self.assertEqual([e['id'] for e in events], [original['id'], attachment['id']])
        self.assertNotIn('선택하지 않은 민감 메모', archive.read('timeline.jsonl').decode())
        self.assertEqual(set(archive.namelist()), {'report.md','manifest.json','timeline.jsonl','attachments/sample.txt'})

    def test_secret_redaction_before_persistence(self):
        # Synthetic markers only, never real credentials.
        for sample in ['password="SYNTHETIC_VALUE"','access_token=SYNTHETIC_VALUE',
                       'Authorization: Bearer SYNTHETIC_VALUE', '{"api_key": "SYNTHETIC_VALUE"}']:
            self.assertNotIn('SYNTHETIC_VALUE', device.redact(sample))
        self.w.event('note', 'password="SYNTHETIC_VALUE"')
        self.assertNotIn('SYNTHETIC_VALUE', (self.w.directory()/'timeline.jsonl').read_text())

    def test_http_origin_and_csrf(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.workbench = self.w
        server.csrf = 'synthetic-csrf-for-test'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f'http://127.0.0.1:{server.server_port}'
        for headers in [{}, {'X-Plipa-CSRF': server.csrf, 'Origin':'https://untrusted.invalid'}]:
            req = urllib.request.Request(url+'/api/note', data=b'{"message":"rejected"}', headers={'Content-Type':'application/json', **headers})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req)
            self.assertEqual(caught.exception.code, 403)
        req = urllib.request.Request(url+'/api/state', headers={'Host':'untrusted.invalid'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(req)
        self.assertEqual(caught.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(url+'/api/attachment?name=../session.json')
        self.assertEqual(caught.exception.code, 404)
        req = urllib.request.Request(url+'/api/note',data=b'{"message":"accepted"}',headers={'Content-Type':'application/json','X-Plipa-CSRF':server.csrf})
        with urllib.request.urlopen(req) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.w.events()),1)


if __name__ == '__main__':
    unittest.main()

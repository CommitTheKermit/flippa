import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import call, patch, Mock
import urllib.request
import urllib.error
import zipfile

import device
import emulator
import ai
from emulator_lifecycle import EmulatorLifecycle
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
            self.w.steps_left = self.w.step_limit
            self.w._loop()
            self.assertIsNotNone(self.w.pending)
            self.assertFalse(self.w.running)
            perform.assert_not_called()
            self.w.cancel()
            self.assertIsNone(self.w.pending)
            self.assertTrue(self.w.stop.is_set())

    def test_ai_request_uses_validated_model_and_reasoning(self):
        with patch.object(self.w, '_spawn'):
            self.w.start_ai('현재 화면 확인', True, model='gpt-5.6-luna', reasoning_effort='low')
        self.assertEqual((self.w.ai_model, self.w.ai_reasoning_effort), ('gpt-5.6-luna', 'low'))
        self.assertEqual((self.w.events()[-1]['ai_model'], self.w.events()[-1]['ai_reasoning_effort']),
                         ('gpt-5.6-luna', 'low'))
        with patch('device.tree', return_value=self.nodes), \
             patch('ai.plan', return_value={'kind': 'done', 'target': 0, 'text': '', 'message': '완료'}) as planner:
            self.w._loop()
        self.assertEqual(planner.call_args.kwargs['model'], 'gpt-5.6-luna')
        self.assertEqual(planner.call_args.kwargs['reasoning_effort'], 'low')
        for model, effort in [('unknown', 'medium'), ('gpt-5.6-terra', 'max')]:
            with self.assertRaises(ValueError):
                self.w.start_ai('현재 화면 확인', True, model=model, reasoning_effort=effort)

    def test_codex_arguments_default_to_terra_and_medium(self):
        process = Mock()
        process.poll.return_value = 0
        process.returncode = 1
        with patch('ai.shutil.which', return_value='codex'), patch('ai.subprocess.Popen', return_value=process) as popen:
            with self.assertRaises(ValueError):
                ai.plan('확인', [], [], threading.Event())
        args = popen.call_args.args[0]
        self.assertEqual(args[args.index('-m') + 1], 'gpt-5.6-terra')
        self.assertIn('model_reasoning_effort="medium"', args)

    def test_record_recovery_after_disconnect(self):
        self.w.event('note', '기존 관찰')
        with patch('device.tree', side_effect=ValueError('연결 끊김')), patch('device.logs', side_effect=ValueError('연결 끊김')):
            self.w.capture()
        restored = Workbench(Path(self.temp.name))
        restored.load('test')
        self.assertEqual(restored.events()[0]['message'], '기존 관찰')
        self.assertEqual(len(restored.events()[-1]['missing']), 2)
        with (restored.directory()/'timeline.jsonl').open('a', encoding='utf-8') as out:
            out.write('{"interrupted":')
        restored.event('note', '복구 뒤 새 기록')
        self.assertEqual(restored.events()[-1]['message'], '복구 뒤 새 기록')

    def test_export_selection_and_linked_step(self):
        original = self.w.event('capture', '선택한 구간')
        self.w.event('note', '선택하지 않은 민감 메모')
        (self.w.directory()/'sample.png').write_bytes(b'example image')
        (self.w.directory()/'sample.txt').write_text('example log', encoding='utf-8')
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
        self.assertNotIn('SYNTHETIC_VALUE', (self.w.directory()/'timeline.jsonl').read_text(encoding='utf-8'))

    def test_low_resource_emulator_launch(self):
        process = Mock()
        with patch('device.avds', return_value=['Flippa_API_33']), \
             patch('device.subprocess.Popen', return_value=process) as popen, \
             patch('device.threading.Thread') as thread:
            self.assertEqual(device.boot('Flippa_API_33'), 'Flippa_API_33')
        args = popen.call_args.args[0]
        for expected in ('-no-window', '-no-audio', '-no-boot-anim', '-no-snapshot', '-grpc-use-token'):
            self.assertIn(expected, args)
        self.assertEqual(args[args.index('-cores') + 1], '2')
        self.assertEqual(args[args.index('-memory') + 1], '3072')
        thread.return_value.start.assert_called_once()

    def test_default_emulator_preparation_is_idempotent_and_reports_ready(self):
        lifecycle = EmulatorLifecycle('Current_Phone_API_37')
        worker = Mock()
        worker.is_alive.return_value = True
        with patch('emulator_lifecycle.device.avds', return_value=['Current_Phone_API_37']), \
             patch('emulator_lifecycle.device.find_avd', return_value=None), \
             patch('emulator_lifecycle.threading.Thread', return_value=worker) as thread:
            self.assertEqual(lifecycle.prepare_default()['phase'], 'starting')
            self.assertEqual(lifecycle.prepare_default()['phase'], 'starting')
        thread.assert_called_once()
        worker.start.assert_called_once()

        ready = {'serial': 'emulator-5554', 'state': 'device'}
        with patch('emulator_lifecycle.device.boot') as boot, \
             patch('emulator_lifecycle.device.find_avd', return_value=ready), \
             patch('emulator_lifecycle.device.boot_completed', return_value=True):
            lifecycle._boot('Current_Phone_API_37')
        boot.assert_called_once_with('Current_Phone_API_37')
        self.assertEqual((lifecycle.phase, lifecycle.serial), ('ready', 'emulator-5554'))

    def test_hung_emulator_is_named_stopped_and_reported(self):
        running = Path(self.temp.name)/'running'
        running.mkdir()
        (running/'pid_111.ini').write_text('port.serial=5554\navd.name=Stale\n', encoding='utf-8')
        current = running/'pid_222.ini'
        current.write_text('port.serial=5554\navd.name=Current_Phone_API_37\n', encoding='utf-8')
        stale = (running/'pid_111.ini').stat().st_mtime
        os.utime(current, (stale + 10, stale + 10))
        with patch.dict('emulator.os.environ', {'PLIPA_EMULATOR_DISCOVERY': str(running)}), \
             patch('device.adb', side_effect=ValueError('console hung')), \
             patch('device.os.kill') as kill:
            self.assertEqual(device.avd_name('emulator-5554'), 'Current_Phone_API_37')
            device.stop('emulator-5554')
        self.assertEqual(kill.call_args.args[0], 222)
        self.assertFalse(current.exists())

        lifecycle = EmulatorLifecycle('Current_Phone_API_37')
        lifecycle.phase, lifecycle.target_name = 'ready', 'Current_Phone_API_37'
        busy = {'serial': 'emulator-5554', 'state': 'device'}
        with patch('emulator_lifecycle.device.avds', return_value=['Current_Phone_API_37']), \
             patch('emulator_lifecycle.device.find_avd', return_value=busy), \
             patch('emulator_lifecycle.device.boot_completed', return_value=False), \
             patch('emulator_lifecycle.device.console_alive', return_value=True), \
             patch('emulator_lifecycle.device.stop') as stop, \
             patch('emulator_lifecycle.threading.Thread') as thread:
            # A busy guest (slow getprop, live console) stays ready and is never killed.
            self.assertEqual(lifecycle.status()['phase'], 'ready')
            lifecycle.phase = 'error'
            lifecycle.prepare_default()
            stop.assert_not_called()
            self.assertEqual(thread.call_args.kwargs['args'], ('Current_Phone_API_37', False))
        lifecycle.phase, lifecycle.worker = 'ready', None
        with patch('emulator_lifecycle.device.avds', return_value=['Current_Phone_API_37']), \
             patch('emulator_lifecycle.device.find_avd', return_value=busy), \
             patch('emulator_lifecycle.device.boot_completed', return_value=False), \
             patch('emulator_lifecycle.device.console_alive', return_value=False), \
             patch('emulator_lifecycle.device.stop') as stop, \
             patch('emulator_lifecycle.threading.Thread') as thread:
            self.assertEqual(lifecycle.status()['phase'], 'error')
            lifecycle.prepare_default()
            stop.assert_called_once_with('emulator-5554')
            self.assertEqual(thread.call_args.kwargs['args'], ('Current_Phone_API_37', True))

    def test_stream_defaults_to_720_and_windows_discovery(self):
        with patch.dict('emulator.os.environ', {}, clear=True):
            self.assertEqual(emulator.stream_size(), 720)
        with patch('emulator.sys.platform', 'win32'), \
             patch.dict('emulator.os.environ', {'LOCALAPPDATA': r'C:\Users\test\AppData\Local'}, clear=True):
            self.assertEqual(emulator.discovery_folders(),
                             [Path(r'C:\Users\test\AppData\Local\Temp\avd\running')])
        legacy = emulator.pb.Image(width=720, height=1280)
        self.assertEqual(emulator.screenshot_size(legacy), (720, 1280))
        current = emulator.pb.Image(width=720, height=1280,
                                    format=emulator.pb.ImageFormat(width=360, height=640))
        self.assertEqual(emulator.screenshot_size(current), (360, 640))


    def test_live_input_ownership_approval_and_release(self):
        connection = Mock()
        self.w.viewers = {'viewer': ('test', connection), 'other': ('test', connection)}
        request = {'session': 'test', 'viewer': 'viewer', 'phase': 'down', 'x': .2, 'y': .3}
        with patch('server.threading.Timer'):
            for invalid in [dict(request, session='old'), dict(request, x=float('nan')),
                            dict(request, x=True), dict(request, phase='move')]:
                with self.assertRaises(ValueError): self.w.live_input(invalid)
            connection.mouse.assert_not_called()
            self.w.live_input(request)
            connection.mouse.assert_called_with(.2, .3, True)
            with self.assertRaises(ValueError): self.w.live_input(dict(request, viewer='other', phase='up'))
            self.w.live_input(dict(request, viewer='other', phase='cancel'))
            self.assertIsNotNone(self.w.pointer)
            self.w.live_input(dict(request, phase='move', y=.8))
            self.w.live_input(dict(request, phase='up', y=.9))
            connection.mouse.assert_called_with(.2, .9, False)
            self.assertIsNone(self.w.pointer)
            self.assertEqual(len(self.w.events()), 1)
            self.assertEqual(self.w.events()[0]['action']['points'][-1], [.2, .9])
            self.w.pending = {'id': 'approval'}
            for data in [request, dict(request, phase='key', key='GoHome')]:
                with self.assertRaises(ValueError): self.w.live_input(data)
            self.w.pending = None
            self.w.live_input(request)
            with patch.object(self.w, '_spawn'):
                self.w.start_ai('현재 화면 확인', True)
            self.assertIsNone(self.w.pointer)
            connection.mouse.assert_called_with(.2, .3, False)
            self.w.live_input(request)
            pointer = self.w.pointer
            self.w.load('test')
            self.assertIsNone(self.w.pointer)
            self.w.live_input(request)
            self.w.pointer_timeout(pointer)  # Old watchdog must not release a newer touch.
            self.assertIsNotNone(self.w.pointer)
            self.w.pointer['updated'] -= 4
            self.w.pointer_timeout(self.w.pointer)
            self.assertIsNone(self.w.pointer)
            self.w.live_input(dict(request, phase='key', key='a'))
            self.assertEqual(self.w.events()[-1]['action']['text'], '[입력 내용 기록 안 함]')
            self.w.live_input(request)
            connection.mouse.side_effect = ValueError('연결 끊김')
            with self.assertRaises(ValueError): self.w.release_pointer()
            self.assertIsNone(self.w.pointer)
            self.assertTrue(self.w.events()[-1]['missing'])

    def test_live_scroll_uses_viewer_and_records_direction(self):
        connection = Mock()
        self.w.viewers = {'viewer': ('test', connection)}
        request = {'session': 'test', 'viewer': 'viewer', 'phase': 'scroll', 'direction': 'down'}
        with patch('server.time.sleep') as sleep:
            self.w.live_input(request)
        connection.mouse.assert_has_calls([call(.5, .72, True), call(.5, .3, False)])
        sleep.assert_called_once_with(.18)
        event = self.w.events()[-1]
        self.assertEqual(event['action'], {'kind': 'scroll', 'direction': 'down', 'steps': 1,
                                           'coordinates': 'normalized'})
        with self.assertRaises(ValueError):
            self.w.live_input(dict(request, direction='sideways'))
        with self.assertRaises(ValueError):
            self.w.live_input(dict(request, session='old'))
        with patch('server.time.sleep') as sleep:
            self.w.live_input(dict(request, direction='top'))
        self.assertEqual(sleep.call_count, 8)
        self.assertEqual(self.w.events()[-1]['action']['steps'], 8)

    def test_stream_frames_and_disconnected_touch_release(self):
        import http.client
        import struct
        import time
        from types import SimpleNamespace
        stopped = threading.Event()
        class Frames:
            def __iter__(self):
                yield SimpleNamespace(format=SimpleNamespace(width=432, height=960), image=b'png-test')
                stopped.wait(5)
            def cancel(self): stopped.set()
        connection = Mock(size=(1080, 2400))
        connection.frames.return_value = Frames()
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.workbench = self.w
        server.csrf = 'synthetic'
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        with patch('server.emulator.Connection', return_value=connection):
            client.request('POST', '/api/stream', json.dumps({'session':'test','viewer':'viewer'}),
                           {'Content-Type':'application/json','X-Plipa-CSRF':'synthetic'})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            length = struct.unpack('!I', response.read(4))[0]
            self.assertEqual(response.read(length), b'png-test')
            self.assertEqual(response.read(4), b'\x00'*4)
            self.w.live_input({'session':'test','viewer':'viewer','phase':'down','x':.5,'y':.5})
            response.close();client.close()
            deadline = time.monotonic()+4
            while self.w.viewers and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertFalse(self.w.viewers)
            self.assertIsNone(self.w.pointer)
            connection.mouse.assert_called_with(.5, .5, False)
            connection.close.assert_called_once()

    def test_preview_stream_does_not_require_a_test_session(self):
        import http.client
        import struct
        from types import SimpleNamespace
        stopped = threading.Event()
        class Frames:
            def __iter__(self):
                yield SimpleNamespace(format=SimpleNamespace(width=432, height=936), image=b'preview')
                stopped.wait(2)
            def cancel(self): stopped.set()
        connection = Mock(size=(1080, 2340))
        connection.frames.return_value = Frames()
        self.w.current = None
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.workbench = self.w
        server.csrf = 'synthetic'
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
        with patch('server.device.devices', return_value=[{'serial':'emulator-5554','state':'device'}]), \
             patch('server.emulator.Connection', return_value=connection):
            client.request('POST', '/api/stream', json.dumps({'serial':'emulator-5554','viewer':'previewer'}),
                           {'Content-Type':'application/json','X-Plipa-CSRF':'synthetic'})
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            length = struct.unpack('!I', response.read(4))[0]
            self.assertEqual(response.read(length), b'preview')
            response.close()
        client.close()

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

        server.public_origin = 'https://vivobook.example.ts.net'
        req = urllib.request.Request(url+'/api/state', headers={
            'Host': 'vivobook.example.ts.net',
            'Origin': 'https://vivobook.example.ts.net',
        })
        with urllib.request.urlopen(req) as response:
            self.assertEqual(response.status, 200)


if __name__ == '__main__':
    unittest.main()

import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from access import AIGate, configured_users
from server import Handler, PrivateHTTPServer, Workspaces, save_json, server_configuration


class UnixClient(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__('plipa.test', timeout=5)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class SharedWorkbenchTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.users = {'owner': {'role': 'owner', 'devices': ['emulator-5554']},
                      'tester': {'role': 'tester', 'devices': ['emulator-5556']}}
        self.registry = Workspaces(self.root, self.users)
        for name, w in self.registry.boards.items():
            w.current = {'id': name+'-test', 'serial': self.users[name]['devices'][0],
                         'package': 'com.example.test', 'environment': {}, 'title': name, 'created': '2026-09-20'}
            w.directory().mkdir()
            save_json(w.directory()/'session.json', w.current)
        self.owner = self.registry.boards['owner']
        self.tester = self.registry.boards['tester']
        self.nodes = [{'id': 0, 'text': '저장', 'bounds': [0, 0, 20, 20], 'enabled': True}]

    def start_http(self):
        self.sock_path = str(self.root/'http.sock')
        server = PrivateHTTPServer(self.sock_path, Handler)
        server.origin = 'https://plipa.test'
        server.workspaces = self.registry
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

    def request(self, user, path, data=None, csrf=None, origin='https://plipa.test'):
        client = UnixClient(self.sock_path)
        headers = {'Host': 'plipa.test', 'Origin': origin}
        if user:
            headers['X-Plipa-User'] = user
        if data is not None:
            headers.update({'Content-Type': 'application/json',
                            'X-Plipa-CSRF': csrf or self.registry.csrf.get(user, '')})
        client.request('GET' if data is None else 'POST', path,
                       None if data is None else json.dumps(data), headers)
        response = client.getresponse()
        payload = response.read()
        status = response.status
        client.close()
        return status, json.loads(payload)

    def test_identity_csrf_record_and_device_isolation(self):
        self.start_http()
        self.owner.event('note', 'owner-only')
        for user in (None, 'unknown'):
            self.assertEqual(self.request(user, '/api/state')[0], 403)
        status, state = self.request('tester', '/api/state')
        self.assertEqual(status, 200)
        self.assertEqual(state['user'], 'tester')
        self.assertEqual([s['id'] for s in state['sessions']], ['tester-test'])
        self.assertEqual(state['events'], [])
        self.assertEqual(state['members'], [])
        self.assertEqual(self.request('tester', '/api/note', {'message': 'x'},
                                     csrf=self.registry.csrf['owner'])[0], 403)
        self.assertEqual(self.request('tester', '/api/state', origin='https://other.test')[0], 403)
        self.assertEqual(self.request('tester', '/api/load', {'id': 'owner-test'})[0], 400)
        with patch('device.environment') as environment:
            self.assertEqual(self.request('tester', '/api/start',
                {'serial': 'emulator-5554', 'package': 'com.example.test', 'title': 'x'})[0], 400)
            environment.assert_not_called()
        status, _ = self.request('tester', '/api/note', {'message': 'tester-only'})
        self.assertEqual(status, 200)
        self.assertEqual(self.tester.events()[0]['user'], 'tester')
        self.assertEqual(len(self.owner.events()), 1)
        self.assertEqual(self.request('tester', '/api/admin/ai', {'user': 'owner', 'allowed': False})[0], 400)
        self.tester.pending = {'id': 'awaiting'}
        self.assertEqual(self.request('owner', '/api/admin/ai', {'user': 'tester', 'allowed': False})[0], 200)
        self.assertIsNone(self.tester.pending)
        self.assertEqual(self.request('tester', '/api/chat', {'goal': '확인', 'consent': True})[0], 400)
        restored = Workspaces(self.root, self.users)
        self.assertFalse(restored.gate.allowed('tester'))
        restored.boards['tester'].load('tester-test')
        self.assertFalse(restored.boards['tester'].running)
        self.assertEqual(restored.boards['tester'].events()[0]['message'], 'tester-only')

    def test_people_and_ai_read_same_debug_evidence_with_log_consent(self):
        self.start_http()
        with patch('device.logs', return_value='app log sample') as logs, patch('device.tree', return_value=self.nodes):
            status, result = self.request('tester', '/api/debug', {'tool': 'logs'})
            self.assertEqual((status, result['result']), (200, 'app log sample'))
            logs.reset_mock()
            request = {'kind': 'logs', 'message': '로그 확인', 'target': 0, 'text': ''}
            done = dict(request, kind='done')
            self.tester.steps_left = 2
            with patch('ai.plan', return_value=request):
                self.tester._loop()
            logs.assert_not_called()
            self.tester.consent_logs = True
            self.tester.steps_left = 2
            with patch('ai.plan', side_effect=[request, done]) as planner:
                self.tester._loop()
                self.assertEqual(planner.call_args.kwargs['evidence']['result'], 'app log sample')
            logs.assert_called_once_with('emulator-5556', 'com.example.test')
            reads = [e for e in self.tester.events() if e.get('tool') == 'logs']
            self.assertEqual([e['actor'] for e in reads], ['human', 'ai'])
            for e in reads:
                evidence = next(x for x in self.tester.events() if x.get('step_id') == e['id'])
                self.assertEqual((self.tester.directory()/evidence['attachments'][0]).read_text(), 'app log sample')

    def test_budget_survives_approval_and_prevents_another_codex_call(self):
        w = self.tester
        action = {'kind': 'tap', 'target': 0, 'text': '', 'message': '저장'}
        with (patch('device.tree', return_value=self.nodes), patch('device.perform') as perform,
              patch('ai.plan', return_value=action) as planner):
            w.goal = '저장 확인'
            w.steps_left = 1
            w._loop()
            self.assertEqual(w.steps_left, 0)
            self.assertIsNotNone(w.pending)
            w.approve(w.pending['id'], True)
            perform.assert_called_once()
            self.assertEqual(planner.call_count, 1)
            self.assertFalse(w.running)
            self.assertIsNone(w.pending)

    def test_global_fifo_queue_cancels_waiter_without_running_it(self):
        gate = AIGate()
        first_stop, second_stop = threading.Event(), threading.Event()
        finished = threading.Event()
        entered = []
        def waiter():
            try:
                with gate.slot('tester', second_stop):
                    entered.append('tester')
            except ValueError:
                pass
            finally:
                finished.set()
        with gate.slot('owner', first_stop):
            thread = threading.Thread(target=waiter)
            thread.start()
            deadline = time.monotonic() + 2
            while not gate.position('tester') and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(gate.position('tester'), 1)
            self.assertEqual(gate.active, 'owner')
            second_stop.set()
            self.assertTrue(finished.wait(2))
            self.assertEqual(entered, [])
        thread.join(2)
        with gate.slot('tester', threading.Event()):
            self.assertEqual(gate.active, 'tester')
        self.assertIsNone(gate.active)

    def test_configuration_rejects_shared_logged_in_device(self):
        with patch.dict(os.environ, {'PLIPA_USERS': json.dumps(self.users)}):
            self.assertEqual(set(configured_users()), {'owner', 'tester'})
        self.users['tester']['devices'] = ['emulator-5554']
        with patch.dict(os.environ, {'PLIPA_USERS': json.dumps(self.users)}):
            with self.assertRaises(ValueError):
                configured_users()

    def test_server_configuration_keeps_single_user_remote_and_shared_auth_separate(self):
        with patch.dict(os.environ, {'PLIPA_ORIGIN': 'https://plipa.test'}, clear=True):
            users, socket_path, origin, shared = server_configuration(4317)
        self.assertEqual(users, {'local': {'role': 'owner', 'devices': None}})
        self.assertIsNone(socket_path)
        self.assertEqual(origin, 'https://plipa.test')
        self.assertFalse(shared)

        with patch.dict(os.environ, {
                'PLIPA_USERS': json.dumps(self.users),
                'PLIPA_ORIGIN': 'https://plipa.test',
        }, clear=True):
            with self.assertRaisesRegex(ValueError, '인증 프록시'):
                server_configuration(4317)

    def test_waiting_request_runs_only_after_current_request_finishes(self):
        gate = AIGate()
        entered = threading.Event()
        def waiter():
            with gate.slot('tester', threading.Event()):
                entered.set()
        with gate.slot('owner', threading.Event()):
            worker = threading.Thread(target=waiter)
            worker.start()
            self.assertFalse(entered.wait(.05))
        self.assertTrue(entered.wait(2))
        worker.join(2)
        self.assertFalse(worker.is_alive())


if __name__ == '__main__':
    unittest.main()

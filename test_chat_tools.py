"""채팅 진입점에서 실제 기존 도구·기록·승인 경계를 검증한다."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request

import chat_tools
from server import Workbench, Handler, ThreadingHTTPServer, save_json


def intent(tool, **values):
    return dict(tool=tool, message='도구를 사용합니다.', goal='', expected='', precondition='', **values)


class ChatToolsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.w = Workbench(Path(self.temp.name))
        self.addCleanup(self.w.close)
        self.w.current = dict(id='test', serial='emulator-test', package='com.example.test',
                              title='수동과 AI 테스트', environment={}, created='2026-10-21')
        self.w.directory().mkdir()
        save_json(self.w.directory()/'session.json', self.w.current)
        self.nodes = [dict(id=0, text='목록', resource='com.example.test:id/list',
                           package='com.example.test', bounds=[0,0,100,200],
                           enabled=True, clickable=True, editable=False, focused=False)]
        for target, value in [('device.tree', self.nodes), ('device.logs', '앱 로그 표본'),
                              ('device.environment', {'package':'com.example.test'})]:
            p = patch(target, return_value=value)
            p.start(); self.addCleanup(p.stop)
        self.explain_observation = chat_tools.explain_observation
        p = patch('chat_tools.explain_observation',return_value='화면에서 목록을 관측했습니다.');p.start();self.addCleanup(p.stop)
        p = patch('qa.ContinuousLogs.start'); p.start(); self.addCleanup(p.stop)

    def chat(self, answer, **options):
        with patch('ai.request_json', return_value=answer):
            self.w.start_ai('사용자의 요청', True, chat_tools=True, **options)
            self.wait_idle()
        return [e for e in self.w.events() if e['kind']=='tool'][-1]

    def wait_idle(self):
        self.w.worker.join(4)
        self.assertFalse(self.w.running)

    def test_http_chat_routes_to_real_read_tool_and_persists_attachment(self):
        http = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        http.workbench = self.w; http.csrf = 'test-csrf'
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        self.addCleanup(http.server_close); self.addCleanup(http.shutdown)
        data = json.dumps({'goal':'화면 구조 확인', 'consent':True, 'tools':True}).encode()
        request = urllib.request.Request(f'http://127.0.0.1:{http.server_port}/api/chat', data=data,
            headers={'Content-Type':'application/json', 'X-Plipa-CSRF':'test-csrf'})
        with patch('ai.request_json', return_value=intent('read_ui')):
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 200)
            self.wait_idle()
        attachment = next(e['attachments'][0] for e in self.w.events() if e.get('attachments'))
        self.assertEqual(json.loads((self.w.directory()/attachment).read_text()), self.nodes)
        self.assertEqual([e for e in self.w.events() if e['kind']=='tool'][-1]['result']['view'], 'structure')
        restored = Workbench(self.w.root); self.addCleanup(restored.close); restored.load('test')
        self.assertEqual([e for e in restored.events() if e['kind']=='tool'][-1]['status'], 'done')

    def test_logs_require_consent_and_dont_leak_into_router_prompt(self):
        self.chat(intent('read_logs'))
        self.assertEqual(self.w.events()[-2]['status'], 'error')
        self.assertFalse(any(e.get('tool')=='logs' and e['kind']=='debug' for e in self.w.events()))
        self.chat(intent('read_logs'), consent_logs=True)
        with patch('ai.request_json',return_value=intent('read_ui')) as request:
            chat_tools.plan_request(self.w, '', '')
        self.assertNotIn('앱 로그 표본', request.call_args.args[0])
        self.assertEqual([e for e in self.w.events() if e['kind']=='tool'][-1]['status'], 'done')

    def test_qa_uses_existing_risk_approval_and_manual_takeover(self):
        answer = intent('run_qa'); answer.update(goal='목록 항목 삭제', expected='삭제 결과 표시')
        action = dict(kind='tap', target=0, text='', message='항목 삭제', effect='destructive')
        with patch('ai.plan',return_value=action), patch('device.perform') as perform:
            result = self.chat(answer)
            self.assertEqual(result['status'], 'waiting')
            self.assertIsNotNone(self.w.pending)
            with self.assertRaises(ValueError):self.w.manual(action)
            perform.assert_not_called()
            self.w.cancel()
            if self.w.cleanup_thread:self.w.cleanup_thread.join(3)
            self.assertIsNone(self.w.pending)
            self.assertTrue(self.w.qa_run.result['cancelled'])
            self.w.manual(action)
            perform.assert_called_once()
        self.assertEqual(self.w.events()[-1]['kind'], 'manual')

    def test_save_uses_previous_actual_qa_not_save_command_and_survives_load(self):
        answer = intent('run_qa'); answer.update(goal='목록 확인', expected='목록이 보인다')
        with patch('ai.plan',return_value=dict(kind='done',message='완료',target=0,text='',effect='navigation')), \
             patch('ai.verify',return_value={'verdict':'pass','reason':'목록 관측','evidence_ids':[]}):
            self.chat(answer)
        self.w.load('test')
        self.chat(intent('save_scenario'))
        saved = self.w.scenarios.list()[0]
        self.assertEqual(saved['goal'], '목록 확인')
        self.assertEqual(saved['expected'], '목록이 보인다')
        self.assertEqual(saved['package'], 'com.example.test')

    def test_report_includes_manual_evidence_and_never_exports_automatically(self):
        self.w.event('manual', '수동으로 목록을 열었다', fact=True)
        self.w.capture()
        with patch.object(self.w, 'export') as export:
            result = self.chat(intent('draft_report'))['result']
            export.assert_not_called()
        draft = result['draft']
        self.assertIn('수동으로 목록을 열었다', draft['steps'])
        self.assertGreaterEqual(len(draft['ids']), 2)
        self.assertEqual(result['view'], 'report')
        self.assertTrue(any(e['id'] in draft['ids'] and e.get('attachments') for e in self.w.events()))

    def test_read_explanation_receives_observation_but_cannot_execute_tools(self):
        with patch('chat_tools.ai.request_json',return_value={'message':'관측 자료가 부족합니다.'}) as request:
            message = self.explain_observation(self.w, 'read_ui', self.nodes)
        self.assertIn('관측 자료', message)
        self.assertIn('com.example.test:id/list', request.call_args.args[0])
        self.assertEqual(request.call_args.args[1], chat_tools.READ_REPLY_SCHEMA)

    def test_stop_during_planning_prevents_tool_effects(self):
        entered = threading.Event()
        def wait(*args):
            entered.set(); self.w.stop.wait(2)
            return intent('capture')
        with patch('ai.request_json',side_effect=wait),patch.object(self.w,'capture') as capture:
            self.w.start_ai('증거 수집',True,chat_tools=True)
            self.assertTrue(entered.wait(2)); self.w.cancel(); self.wait_idle()
            capture.assert_not_called()
        self.assertEqual([e for e in self.w.events() if e['kind']=='tool'][-1]['status'], 'cancelled')

    def test_failed_explanation_preserves_completed_read_and_its_file(self):
        with patch('chat_tools.explain_observation',side_effect=ValueError('모델 응답 실패')):
            result = self.chat(intent('read_ui'))
        self.assertEqual(result['status'], 'done')
        self.assertTrue(any(e.get('attachments') for e in self.w.events()))
        self.assertEqual(self.w.events()[-1]['kind'], 'error')

    def test_stop_during_explanation_does_not_publish_late_reply(self):
        entered = threading.Event()
        def explain(*args):
            entered.set(); self.w.stop.wait(2)
            return '중지 뒤에 보이면 안 되는 답변'
        with patch('ai.request_json',return_value=intent('read_ui')), \
             patch('chat_tools.explain_observation',side_effect=explain):
            self.w.start_ai('화면 확인',True,chat_tools=True)
            self.assertTrue(entered.wait(2));self.w.cancel();self.wait_idle()
        self.assertFalse(any(e['message']=='중지 뒤에 보이면 안 되는 답변' for e in self.w.events()))
        self.assertTrue(any(e.get('attachments') for e in self.w.events()))

    def test_unsupported_tool_and_missing_expected_cannot_execute(self):
        for answer in [intent('shell'), intent('run_qa')]:
            with patch('device.perform') as perform:
                self.chat(answer)
                perform.assert_not_called()
                self.assertIsNone(self.w.qa_run)
                self.assertEqual([e for e in self.w.events() if e['kind']=='tool'][-1]['status'],'error')

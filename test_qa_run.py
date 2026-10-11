import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
import zipfile

import ai
import device
import external_agent
from plipa_mcp import rpc, LocalWorkbenchClient
from qa import ScenarioStore, approval_reason, ContinuousLogs
from qa_run import QARun
from server import Workbench, Handler, ThreadingHTTPServer, save_json


class QAWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.w = Workbench(Path(self.temp.name))
        self.w.current = {'id':'session','serial':'emulator-5554','package':'com.example.test',
                          'title':'테스트','environment':{}}
        self.w.directory().mkdir()
        save_json(self.w.directory()/'session.json', self.w.current)
        self.nodes = [{'id':0,'text':'목록','resource':'com.example.test:id/list','package':'com.example.test',
                       'bounds':[0,0,100,100],'enabled':True,'clickable':True,'editable':False,'focused':False}]
        self.action = {'kind':'tap','target':0,'text':'','message':'목록 열기','effect':'navigation'}
        self.addCleanup(self.w.close)
        for target, value in [('device.tree',self.nodes),('device.display_size',[100,200])]:
            p = patch(target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch('qa.ContinuousLogs.start')
        p.start()
        self.addCleanup(p.stop)

    def run_setup(self, **kwargs):
        self.w.goal = '목록 열기'
        self.w.qa_run = QARun(self.w, **kwargs)
        self.w.steps_left = 1
        self.w.running = True
        return self.w.qa_run

    def wait_idle(self):
        deadline = time.monotonic()+4
        while self.w.running and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(self.w.running)

    def test_navigation_executes_and_last_action_gets_separate_verification(self):
        run = self.run_setup(expected='목록이 표시된다')
        with patch('ai.plan',return_value=self.action) as planner, patch('device.perform') as perform, \
             patch('ai.verify',return_value={'verdict':'pass','reason':'목록 관측','evidence_ids':[]}) as verify:
            self.w._loop()
        perform.assert_called_once()
        planner.assert_called_once()
        verify.assert_called_once()
        self.assertEqual(self.w.steps_left,0)
        self.assertTrue(run.finished)
        self.assertIsNone(self.w.pending)
        self.assertTrue(any(e['kind']=='verdict' for e in self.w.events()))
        step = next(e for e in self.w.events() if e['kind']=='step')
        related = [e for e in self.w.events() if e.get('step_id')==step['id']]
        self.assertTrue(any(e['message']=='조작 전 증거' for e in related))
        self.assertTrue(any(e['message']=='조작 후 증거' for e in related))
        self.assertTrue(any(e['message']=='조작 구간 로그' for e in related))
        blob = self.w.export({'ids':[step['id']],'title':'x','steps':'x','expected':'x','actual':'x','ui':True,'logs':True})
        archive=zipfile.ZipFile(io.BytesIO(blob))
        self.assertTrue(any(n.endswith('-logcat.txt') for n in archive.namelist()))
        self.assertGreaterEqual(sum(n.endswith('-ui.json') for n in archive.namelist()),2)

    def test_external_writes_wait_for_human_and_verify_after_last_approval(self):
        self.run_setup(expected='전송 결과 표시')
        action = dict(self.action,effect='external_write')
        with patch('ai.plan',return_value=action), patch('device.perform') as perform, \
             patch('ai.verify',return_value={'verdict':'inconclusive','reason':'서버 증거 없음','evidence_ids':[]}) as verify:
            self.w._loop()
            perform.assert_not_called()
            pending=self.w.pending['id']
            self.w.approve(pending,True)
            self.wait_idle()
            perform.assert_called_once()
            verify.assert_called_once()
            with self.assertRaises(ValueError):self.w.approve(pending,True)

    def test_cancel_during_approved_action_does_not_resume_ai(self):
        run=self.run_setup(expected='결과 표시')
        with patch('ai.plan',return_value=dict(self.action,effect='destructive')):
            self.w._loop()
        with patch('device.perform',side_effect=lambda *a,**k:self.w.cancel()), patch.object(self.w,'_spawn') as spawn:
            self.w.approve(self.w.pending['id'],True)
        spawn.assert_not_called()
        self.assertFalse(self.w.running)
        self.assertTrue(run.finished)
        self.assertEqual(run.result['verdict'],'inconclusive')

    def test_failed_approval_observation_releases_device(self):
        self.run_setup(expected='결과 표시')
        with patch('ai.plan',return_value=dict(self.action,effect='destructive')):
            self.w._loop()
        with patch('device.tree',side_effect=ValueError('연결 끊김')),patch('device.perform') as perform:
            with self.assertRaises(ValueError):self.w.approve(self.w.pending['id'],True)
        perform.assert_not_called()
        self.assertFalse(self.w.running)
        self.assertIsNone(self.w.pending)

    def test_saved_verdict_restores_but_mcp_permission_does_not(self):
        run=self.run_setup()
        run.finish('완료',verify=False)
        self.w.running=False
        self.w.external_enabled=True
        restored=Workbench(self.w.root)
        restored.load('session')
        self.assertEqual(restored.state()['result']['run_id'],run.id)
        self.assertFalse(restored.state()['external_enabled'])
        restored.close()

    def test_policy_checks_package_unknown_effect_and_destructive_labels(self):
        package='com.example.test'
        self.assertIsNone(approval_reason(self.action,self.nodes,package,True))
        for action,nodes in [(dict(self.action,effect='unknown'),self.nodes),
                             (self.action,[dict(self.nodes[0],text='삭제')]),
                             (self.action,[dict(self.nodes[0],package='other.app')]),
                             (dict(self.action,kind='key',text='ENTER'),self.nodes)]:
            self.assertIsNotNone(approval_reason(action,nodes,package,True))

    def test_changed_screen_and_cancel_do_not_execute(self):
        run=self.run_setup()
        observed=run.observe()
        with patch('device.tree',return_value=[dict(self.nodes[0],text='다른 화면')]), patch('device.perform') as perform:
            with self.assertRaises(ValueError):run.propose(self.action,observed)
            perform.assert_not_called()
        self.w.stop.set()
        with patch('device.perform') as perform:
            self.assertFalse(run.propose(self.action,observed))
            perform.assert_not_called()
        self.w.running=False

    def test_screenshot_is_only_captured_and_forwarded_with_consent(self):
        for consent in (False,True):
            run=self.run_setup(screens=consent)
            with patch('device.screenshot',return_value=b'fake PNG') as screenshot, \
                 patch('ai.plan',return_value=dict(self.action,kind='done')) as planner:
                self.w._loop()
            self.assertEqual(screenshot.called,consent)
            self.assertEqual(planner.call_args.kwargs['image_path'] is not None,consent)
            self.assertTrue(run.finished)

    def test_missing_final_evidence_is_inconclusive(self):
        run=self.run_setup(expected='결과 확인')
        with patch('device.tree',side_effect=ValueError('연결 끊김')),patch('ai.verify') as verify:
            run.finish('완료')
        verify.assert_not_called()
        self.assertEqual(run.result['verdict'],'inconclusive')
        self.w.running=False

    def test_scenarios_survive_restart_and_are_separate_per_workspace(self):
        saved=self.w.scenarios.save({'title':'검색','goal':'1. 목록 열기\n2. 검색','expected':'결과 표시','precondition':'로그인'},'com.example.test')
        restored=ScenarioStore(self.w.root)
        self.assertEqual(restored.get(saved['id'])['goal'],'1. 목록 열기\n2. 검색')
        self.assertEqual(ScenarioStore(self.w.root/'other').list(),[])
        with self.assertRaises(ValueError):restored.get('../session')
        with self.assertRaises(ValueError):restored.save({'title':'x','goal':'password=SYNTHETIC_VALUE','expected':'x'},'com.example.test')

    def test_mcp_requires_web_permission_and_shares_approval_and_lock(self):
        with self.assertRaises(ValueError):external_agent.dispatch(self.w,{'command':'begin','goal':'x','expected':'x'})
        self.w.external_enabled=True
        begun=external_agent.dispatch(self.w,{'command':'begin','goal':'목록 열기','expected':'목록 표시'})
        rid=begun['run_id']
        with self.assertRaises(ValueError):self.w.manual(self.action)
        observed=external_agent.dispatch(self.w,{'command':'observe','run_id':rid})
        self.assertNotIn('image',observed)
        with self.assertRaises(ValueError):external_agent.dispatch(self.w,{'command':'logs','run_id':rid})
        request={'command':'act','run_id':rid,'observation_id':observed['id'],
                 'action':dict(self.action,effect='destructive')}
        with patch('device.perform') as perform:
            result=external_agent.dispatch(self.w,request)
            perform.assert_not_called()
            self.assertIsNotNone(result['pending'])
            self.w.approve(result['pending']['id'],True)
            perform.assert_called_once()
        with patch('ai.verify',return_value={'verdict':'inconclusive','reason':'확인 불가','evidence_ids':[]}):
            result=external_agent.dispatch(self.w,{'command':'finish','run_id':rid})
        self.assertFalse(self.w.running)
        self.assertEqual(result['verdict'],'inconclusive')
        with self.assertRaises(ValueError):external_agent.dispatch(self.w,request)

    def test_mcp_expiry_returns_control_and_preserves_evidence(self):
        self.w.external_enabled=True
        external_agent.dispatch(self.w,{'command':'begin','goal':'x','expected':'x'})
        run=self.w.qa_run
        run.last_activity-=121
        run._expire()
        self.assertTrue(run.finished)
        self.assertFalse(self.w.running)
        self.assertEqual(run.result['verdict'],'inconclusive')

    def test_mcp_stdio_protocol_and_image_response(self):
        client=Mock()
        client.call.return_value={'id':'observation','image':base64.b64encode(b'png').decode()}
        initialized=rpc({'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},client)
        self.assertEqual(initialized['result']['protocolVersion'],'2025-06-18')
        self.assertIsNone(rpc({'jsonrpc':'2.0','method':'notifications/initialized'},client))
        result=rpc({'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'plipa_observe','arguments':{'run_id':'r'}}},client)
        self.assertEqual(result['result']['content'][1]['type'],'image')
        with self.assertRaises(ValueError):LocalWorkbenchClient('http://external.example')
        denied=rpc({'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'plipa_act','arguments':{'command':'approve'}}},client)
        self.assertIn('error',denied)

    def test_actual_http_mcp_bridge_and_scenario_api(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.workbench=self.w
        server.csrf='synthetic-csrf'
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client=LocalWorkbenchClient('http://127.0.0.1:'+str(server.server_port))
        with self.assertRaises(ValueError):client.call('begin',{'goal':'x','expected':'x'})
        client._request('/api/agent/settings',{'enabled':True,'consent':True},server.csrf)
        rid=client.call('begin',{'goal':'x','expected':'x'})['run_id']
        self.assertEqual(client.call('observe',{'run_id':rid})['nodes'],self.nodes)
        saved=client._request('/api/scenarios/save',{'title':'x','goal':'목록 보기','expected':'목록 표시'},server.csrf)
        self.assertEqual(self.w.scenarios.get(saved['id'])['goal'],'목록 보기')
        client.close()
        self.wait_idle()
        self.assertTrue(self.w.qa_run.finished)

    def test_verifier_rejects_fabricated_evidence_references(self):
        with patch('ai.request_json',return_value={'verdict':'pass','reason':'x','evidence_ids':['invented']}):
            with self.assertRaises(ValueError):ai.verify('x',[{'id':'real'}],threading.Event())
        with patch('ai.request_json',return_value={'verdict':'pass','reason':'x','evidence_ids':[]}):
            self.assertEqual(ai.verify('x',[{'id':'real'}],threading.Event())['verdict'],'inconclusive')

    def test_collected_logs_remain_readable_after_app_process_exits(self):
        run=self.run_setup()
        run.evidence.logs.lines.append({'sequence':1,'line':'crash evidence','received':'now'})
        run.evidence.logs.sequence=1
        with patch('device.logs',side_effect=ValueError('앱 종료')) as sample:
            result=run.evidence.read_logs()
        sample.assert_not_called()
        self.assertEqual(result['lines'][0]['line'],'crash evidence')
        run.finish('테스트 종료',verify=False)
        self.w.running=False

    def test_continuous_logs_are_redacted_and_interval_reports_buffer_loss(self):
        collector=ContinuousLogs()
        collector.process=Mock()
        collector.process.stdout=io.BytesIO(b'123 package password=SYNTHETIC_VALUE\n124 ordinary\n')
        path=Path(self.temp.name)/'logs.txt'
        collector._collect(path)
        self.assertNotIn('SYNTHETIC_VALUE',path.read_text())
        self.assertEqual(len(collector.since(1)['lines']),1)
        self.assertTrue(collector.since(1)['missing'])
        collector.lines.clear()
        collector.lines.append({'sequence':5002,'line':'latest'})
        self.assertIn('단계 로그 버퍼 상한 초과',collector.since(0)['missing'])

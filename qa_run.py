"""내장 AI와 MCP가 공유하는 테스트 실행·승인·최종 검증 흐름."""
import time
import uuid
import threading

import ai
import device
from qa import RunEvidence, approval_reason, timestamp


class QARun:
    def __init__(self, w, expected='', precondition='', autonomous=True, screens=False, external=False):
        self.w = w
        self.id = uuid.uuid4().hex
        self.expected = expected
        self.precondition = precondition
        self.autonomous = autonomous
        self.screens = screens
        self.external = external
        self.evidence = RunEvidence(w, self.id, screens)
        self.started = False
        self.finished = False
        self.observations = []
        self.result = None
        self.highlight = None
        self.last_observation = None
        self.last_activity = time.monotonic()
        self.timer = None
        self.finish_lock = threading.Lock()

    def begin(self):
        if not self.started:
            self.started = True
            self.w.event('run', 'AI 테스트 시작', run_id=self.id, expected=self.expected,
                         precondition=self.precondition, autonomous=self.autonomous,
                         images_ai_consent=self.screens, actor='mcp' if self.external else 'ai', fact=True)
            self.evidence.start()
        if self.external:
            self.renew()

    def renew(self):
        self.last_activity = time.monotonic()
        if self.timer:
            self.timer.cancel()
        self.timer = threading.Timer(120, self._expire)
        self.timer.daemon = True
        self.timer.start()

    def _expire(self):
        with self.w.operation:
            if self.finished or time.monotonic()-self.last_activity < 119:
                return
            self.w.stop.set()
            self.w.pending = None
            self.finish('MCP 연결이 120초 동안 갱신되지 않았습니다.', verify=False)
            self.w.running = False

    def observe(self, phase='현재 화면', step_id=None):
        observed = self.evidence.observe(phase, step_id)
        self.last_observation = observed
        self.observations.append({k: v for k, v in observed.items() if k != 'screen_path'})
        return observed

    def propose(self, action, observed):
        w = self.w
        nodes = observed['nodes']
        if observed['missing'] and not nodes:
            raise ValueError('현재 화면을 읽지 못해 조작을 중지했습니다.')
        reason = approval_reason(action, nodes, w.session()['package'], self.autonomous)
        if action['kind'] == 'tap':
            target = device.node_for(action, nodes)
            try:
                viewport = device.display_size(w.session()['serial'])
                self.highlight = {'bounds': target['bounds'], 'viewport': viewport,
                                  'label': target['text'], 'expires': time.time()+4}
            except ValueError:
                self.highlight = None
        with w.lock:
            if w.stop.is_set():
                return False
            w.event('ai', action['message'], run_id=self.id, action=action, fact=False)
            if reason:
                w.pending = {'id': uuid.uuid4().hex, 'action': action, 'nodes': nodes,
                             'created': timestamp(), 'reason': reason, 'run_id': self.id}
                return False
        self.execute(action, nodes, approved=False)
        return True

    def execute(self, action, nodes, approved):
        w = self.w
        fresh = device.tree(w.session()['serial'])
        if fresh != nodes:
            raise ValueError('화면이 변경되어 조작을 실행하지 않았습니다. 다시 관찰하세요.')
        step = w.event('step', '조작 전후 확인', run_id=self.id, fact=True)
        cursor = self.evidence.logs.mark()
        self.observe('조작 전', step['id'])
        with w.lock:
            if w.stop.is_set() or not w.ai_gate.allowed(w.user):
                raise ValueError('AI 실행이 중지됐습니다.')
            # Capture can take time; recheck immediately before applying the input.
            fresh = device.tree(w.session()['serial'])
            if fresh != nodes:
                raise ValueError('증거 수집 중 화면이 변경되어 조작을 중지했습니다.')
            if action['kind'] == 'text' and not device.node_for(action, fresh).get('focused'):
                raise ValueError('입력 대상에 초점이 없습니다. 먼저 입력란을 선택하세요.')
            w.debug.perform(action, fresh, actor='mcp' if self.external else 'ai',
                            approved=approved, step_id=step['id'], run_id=self.id)
        w.stop.wait(.4)
        self.observe('조작 후', step['id'])
        interval = self.evidence.interval(step['id'], cursor)
        if w.consent_logs:
            self.observations.append({'id': step['id'], 'logs': interval['lines'][-200:], 'missing': interval['missing']})

    def run(self):
        w = self.w
        self.begin()
        tool_evidence = None
        while w.steps_left > 0 and not w.stop.is_set():
            with w.operation:
                observed = self.observe()
            if not observed['nodes']:
                raise ValueError('화면 구조를 읽지 못했습니다.')
            history = [{'kind': e['kind'], 'message': e['message'], 'action': e.get('action')}
                       for e in w.events() if e.get('run_id') == self.id and e['kind'] in ('ai','action','error')]
            w.steps_left -= 1
            action = ai.plan(w.goal, observed['nodes'], history, w.stop, evidence=tool_evidence,
                             logs_allowed=w.consent_logs, model=w.ai_model, reasoning_effort=w.ai_reasoning_effort,
                             image_path=observed['screen_path'], expected=self.expected, precondition=self.precondition)
            if w.stop.is_set():
                break
            if action['kind'] == 'done':
                self.finish(action['message'])
                return
            if action['kind'] in ('logs', 'environment'):
                if action['kind'] == 'logs' and not w.consent_logs:
                    raise ValueError('앱 로그 AI 전송에 동의하지 않았습니다.')
                with w.operation:
                    tool_evidence = {'tool': action['kind'], 'result': self.evidence.read_logs() if action['kind'] == 'logs' else w.debug.read('environment', actor='ai')}
                    if action['kind'] == 'logs':
                        tool_evidence['continuous'] = self.evidence.logs.since(max(0, self.evidence.logs.mark()-200))
                continue
            if action['kind'] == 'observe':
                w.stop.wait(.5)
                continue
            with w.operation:
                if not self.propose(action, observed):
                    return
        if not w.pending:
            self.finish('사용자 중지' if w.stop.is_set() else '조작 호출 한도 도달', verify=not w.stop.is_set())

    def finish(self, reason, verify=True):
        with self.finish_lock:
            return self._finish(reason, verify)

    def _finish(self, reason, verify):
        if self.finished:
            return self.result
        w = self.w
        result = {'verdict': 'inconclusive', 'reason': reason, 'evidence_ids': []}
        try:
            if verify and not w.stop.is_set() and self.expected:
                with w.operation:
                    final = self.observe('최종 결과')
                if final['missing'] or not final['nodes']:
                    result['reason'] = '최종 관측 증거가 부족합니다.'
                else:
                    # The verifier has a separate single call budget, including after the last input.
                    result = ai.verify(self.expected, self.observations[-24:], w.stop,
                                       model=w.ai_model, reasoning_effort=w.ai_reasoning_effort,
                                       image_path=final['screen_path'])
            elif not self.expected and verify:
                result['reason'] = '기대 결과를 입력하지 않아 합격 여부를 판정하지 않았습니다.'
        except (ValueError, OSError) as exc:
            result = {'verdict': 'inconclusive', 'reason': device.redact(str(exc)), 'evidence_ids': []}
        finally:
            self.finished = True
            self.highlight = None
            if self.timer:
                self.timer.cancel()
            self.evidence.close()
        self.result = {**result, 'run_id': self.id, 'expected': self.expected, 'finished': timestamp()}
        w.event('verdict', {'pass':'통과','fail':'실패','inconclusive':'확인 불가'}[result['verdict']]+': '+result['reason'],
                result=self.result, run_id=self.id, fact=False)
        return self.result

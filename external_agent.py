"""로컬 MCP 클라이언트가 현재 웹 작업실의 실행 경로를 공유한다."""
import base64

import ai
from qa import clean_text
from qa_run import QARun


def dispatch(w, data):
    if not w.external_enabled:
        raise ValueError('웹에서 현재 작업실의 MCP 연결을 먼저 허용하세요.')
    if not w.ai_gate.allowed(w.user):
        raise ValueError('AI 사용이 중지돼 있습니다.')
    command = data.get('command')
    if command == 'status':
        run = w.qa_run
        if run and run.external and not run.finished:
            run.renew()
        return {'session': w.session(), 'running': w.running, 'pending': w.pending,
                'run_id': run.id if run and run.external else None,
                'result': run.result if run else None}
    if command == 'begin':
        if w.running or w.pending:
            raise ValueError('진행 중인 AI 테스트를 먼저 종료하세요.')
        goal = clean_text(data.get('goal'))
        expected = clean_text(data.get('expected'))
        precondition = clean_text(data.get('precondition', ''), optional=True)
        w.release_pointer()
        w.stop.clear()
        w.goal = goal
        w.qa_run = QARun(w, expected, precondition, autonomous=w.external_autonomous, screens=w.external_screens, external=True)
        w.running = True
        w.consent_logs = w.external_logs
        w.event('user', goal, actor='mcp', fact=False)
        w.qa_run.begin()
        return {'run_id': w.qa_run.id}
    run = w.qa_run
    if not run or not run.external or run.finished or data.get('run_id') != run.id:
        raise ValueError('현재 MCP 실행 ID를 확인하세요.')
    run.renew()
    if command == 'finish':
        if w.pending:
            raise ValueError('보류 중인 조작을 웹에서 확인하거나 취소하세요.')
        with w.ai_gate.slot(w.user, w.stop):
            result = run.finish('외부 에이전트 실행 종료')
        w.running = False
        return result
    if command == 'cancel':
        w.cancel()
        return {'cancelled': True}
    if w.pending:
        return {'pending': w.pending, 'message': '웹에서 사람이 승인할 때까지 기다리세요. status로 조회할 수 있습니다.'}
    if command == 'observe':
        observed = run.observe()
        result = {k: v for k, v in observed.items() if k != 'screen_path'}
        if observed['screen_path']:
            result['image'] = base64.b64encode(observed['screen_path'].read_bytes()).decode('ascii')
        return result
    if command in ('logs', 'environment'):
        if command == 'logs' and not w.external_logs:
            raise ValueError('웹에서 MCP 로그 전송을 허용하세요.')
        return {'result': run.evidence.read_logs(actor='mcp') if command == 'logs' else w.debug.read(command, actor='mcp')}
    if command == 'act':
        action = data.get('action')
        ai.validate_action(action)
        if action['kind'] not in ('tap','text','key','scroll'):
            raise ValueError('입력 조작만 act로 요청하세요.')
        observed = run.last_observation
        if not observed or observed['id'] != data.get('observation_id'):
            raise ValueError('최신 observe 결과의 ID를 사용하세요.')
        performed = run.propose(action, observed)
        if w.pending:
            w.running = False
        return {'performed': performed, 'pending': w.pending}
    raise ValueError('지원하지 않는 MCP 명령입니다.')

"""사람과 AI가 함께 사용하는 관찰·조작 및 증거 기록."""
import json
import device


def redact_result(value):
    if isinstance(value, str):
        return device.redact(value)
    if isinstance(value, list):
        return [redact_result(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_result(item) for key, item in value.items()}
    return value


class DebugTools:
    READS = ('ui', 'logs', 'environment')

    def __init__(self, session, directory, record):
        self.session = session
        self.directory = directory
        self.record = record

    def read(self, kind, actor='human', step=None):
        s = self.session()
        collectors = {
            'ui': lambda: device.tree(s['serial']),
            'logs': lambda: device.logs(s['serial'], s['package']),
            'environment': lambda: device.environment(s['serial'], s['package']),
        }
        if kind not in collectors:
            raise ValueError('지원하지 않는 디버깅 기능입니다.')
        step = step or self.record('debug', {'ui': '화면 구조 조회', 'logs': '앱 로그 조회',
                           'environment': '앱·기기 정보 조회'}[kind], actor=actor, tool=kind, fact=True)
        try:
            result = redact_result(collectors[kind]())
            payload = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, indent=2)
            name = step['id'] + ('-logcat.txt' if kind == 'logs' else f'-{kind}.json')
            (self.directory()/name).write_text(payload)
        except ValueError as exc:
            self.record('evidence', '조회 실패', step_id=step['id'], actor=actor,
                        missing=[str(exc)], fact=True)
            raise
        self.record('evidence', '조회 결과', step_id=step['id'], actor=actor,
                    attachments=[name], missing=[], fact=True)
        return result

    def perform(self, action, nodes=None, actor='human', approved=False):
        device.perform(self.session()['serial'], action, nodes)
        safe = dict(action)
        if safe.get('kind') == 'text':
            safe['text'] = '[입력 내용 기록 안 함]'
        return self.record('manual' if actor == 'human' else 'action',
                           '수동 조작' if actor == 'human' else 'AI 조작 실행',
                           action=safe, actor=actor, approved=approved, fact=True)

    def launch(self):
        s = self.session()
        device.launch(s['serial'], s['package'])
        self.record('manual', '대상 앱 실행', actor='human', fact=True)

"""테스트 실행 정책, 시나리오 저장, 연속 로그와 단계별 관측 증거."""
from collections import deque
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import threading
import uuid

import device


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def clean_text(value, limit=3000, optional=False):
    if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
        raise ValueError('테스트 입력 내용을 확인하세요.')
    if device.redact(value) != value:
        raise ValueError('비밀값을 테스트 입력에 저장할 수 없습니다.')
    return value.strip()


class ScenarioStore:
    def __init__(self, root):
        self.root = Path(root)/'scenarios'

    def list(self):
        result = []
        for path in sorted(self.root.glob('*.json')):
            try:
                result.append(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                continue
        return result

    def get(self, sid):
        if not isinstance(sid, str) or not re.fullmatch(r'[a-f0-9]{32}', sid):
            raise ValueError('시나리오 ID를 확인하세요.')
        try:
            return json.loads((self.root/(sid+'.json')).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ValueError('저장된 시나리오를 찾지 못했습니다.') from None

    def save(self, data, package):
        scenario = {'id': uuid.uuid4().hex, 'package': package, 'created': timestamp(),
                    'title': clean_text(data.get('title'), 120),
                    'goal': clean_text(data.get('goal')),
                    'precondition': clean_text(data.get('precondition', ''), optional=True),
                    'expected': clean_text(data.get('expected'))}
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.root/(scenario['id']+'.json')
        path.write_text(json.dumps(scenario, ensure_ascii=False, indent=2), encoding='utf-8')
        return scenario


def approval_reason(action, nodes, package, autonomous):
    """모델의 영향 분류와 실제 대상·화면을 함께 검사한다. 모호한 입력은 승인한다."""
    if action['kind'] in ('observe', 'logs', 'environment', 'done'):
        return None
    if not autonomous:
        return '개별 승인 모드입니다.'
    if action.get('effect') not in ('navigation', 'local_input'):
        return '외부 영향·파괴적 변경 또는 영향이 불확실한 조작입니다.'
    packages = {n.get('package') for n in nodes if n.get('package')}
    if package not in packages or packages - {package, 'com.android.systemui'}:
        return '테스트 앱 밖의 화면이거나 현재 앱을 확인할 수 없습니다.'
    if action['kind'] == 'key' and action['text'] not in ('BACK', 'DEL'):
        return 'Enter·Home 키는 전송이나 앱 이탈을 일으킬 수 있습니다.'
    if action['kind'] in ('tap', 'text'):
        target = device.node_for(action, nodes)
        if target.get('package') != package:
            return '테스트 앱에 속한 대상이 아닙니다.'
        context = ' '.join(str(target.get(k, '')) for k in ('text', 'resource', 'class'))
        if re.search(r'전송|보내|삭제|탈퇴|결제|구매|주문|등록|게시|저장|구독|로그아웃|send|submit|delete|remove|pay|purchase|publish|save|sign.?out', context, re.I):
            return '전송·저장·삭제 등 확인이 필요한 대상입니다.'
        if action['kind'] == 'text' and not target.get('editable'):
            return '입력란을 확인할 수 없습니다.'
    return None


class ContinuousLogs:
    """앱 UID로 한정한 logcat 스트림. PID가 바뀌어도 같은 앱 로그를 수집한다."""
    def __init__(self):
        self.process = None
        self.thread = None
        self.lock = threading.Lock()
        self.lines = deque(maxlen=5000)
        self.sequence = 0
        self.missing = []
        self.stopped = threading.Event()
        self.saved_bytes = 0

    def start(self, serial, package, path):
        try:
            info = device.adb(serial, 'shell', 'dumpsys', 'package', package)
            found = re.search(r'\buserId=(\d+)\b', info)
            if not found:
                raise ValueError('앱 UID를 확인할 수 없어 지속 로그를 수집하지 못했습니다.')
            uid = found.group(1)
            self.process = subprocess.Popen([device.ADB, '-s', serial, 'logcat', '--uid='+uid,
                                            '-v', 'epoch', '-T', '1'], stdout=subprocess.PIPE,
                                           stderr=subprocess.DEVNULL)
            self.thread = threading.Thread(target=self._collect, args=(Path(path),), daemon=True)
            self.thread.start()
        except (OSError, ValueError) as exc:
            self.missing.append(device.redact(str(exc)))

    def _collect(self, path):
        try:
            with path.open('w', encoding='utf-8') as out:
                while not self.stopped.is_set():
                    raw = self.process.stdout.readline(65536)
                    if not raw:
                        break
                    line = device.redact(raw.decode('utf-8', errors='replace').rstrip())
                    with self.lock:
                        self.sequence += 1
                        item = {'sequence': self.sequence, 'received': timestamp(), 'line': line}
                        self.lines.append(item)
                    encoded = json.dumps(item, ensure_ascii=False)+'\n'
                    self.saved_bytes += len(encoded.encode('utf-8'))
                    if self.saved_bytes <= 10*1024*1024:
                        out.write(encoded)
                        out.flush()
                    elif '지속 로그 저장 상한 10MiB 초과' not in self.missing:
                        self.missing.append('지속 로그 저장 상한 10MiB 초과')
            if not self.stopped.is_set():
                self.missing.append('logcat 스트림 종료: 이후 로그가 누락될 수 있습니다.')
        except OSError:
            self.missing.append('지속 로그 파일 저장 실패')

    def mark(self):
        with self.lock:
            return self.sequence

    def since(self, cursor):
        with self.lock:
            missing = list(self.missing)
            if self.lines and cursor < self.lines[0]['sequence']-1:
                missing.append('단계 로그 버퍼 상한 초과')
            return {'lines': [x for x in self.lines if x['sequence'] > cursor], 'missing': missing}

    def close(self):
        self.stopped.set()
        if self.process:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(2)
            if self.thread:
                self.thread.join(3)
            if self.process.stdout:
                self.process.stdout.close()


class RunEvidence:
    def __init__(self, workbench, run_id, screens):
        self.w = workbench
        self.run_id = run_id
        self.screens = screens
        self.logs = ContinuousLogs()
        self.log_name = run_id+'-continuous-logcat.txt'

    def start(self):
        s = self.w.session()
        self.logs.start(s['serial'], s['package'], self.w.directory()/self.log_name)

    def observe(self, phase, step_id=None):
        entry = self.w.event('observation', phase, run_id=self.run_id, step_id=step_id, fact=True)
        attachments, missing = [], []
        nodes, screen_path = [], None
        try:
            nodes = device.tree(self.w.session()['serial'])
            name = entry['id']+'-ui.json'
            (self.w.directory()/name).write_text(json.dumps(nodes, ensure_ascii=False), encoding='utf-8')
            attachments.append(name)
        except ValueError as exc:
            missing.append(str(exc))
        if self.screens:
            try:
                name = entry['id']+'-screen.png'
                path = self.w.directory()/name
                path.write_bytes(device.screenshot(self.w.session()['serial']))
                attachments.append(name)
                screen_path = path
            except ValueError as exc:
                missing.append(str(exc))
        self.w.event('evidence', phase+' 증거', run_id=self.run_id, step_id=step_id or entry['id'],
                     observation_id=entry['id'], attachments=attachments, missing=missing, fact=True)
        return {'id': entry['id'], 'nodes': nodes, 'missing': missing, 'screen_path': screen_path}

    def read_logs(self, actor='ai'):
        recent = self.logs.since(max(0, self.logs.mark()-200))
        if not recent['lines'] and not self.logs.process:
            try:
                return self.w.debug.read('logs', actor=actor)
            except ValueError as exc:
                recent['missing'].append(str(exc))
        entry = self.w.event('debug', '지속 수집한 앱 로그 조회', run_id=self.run_id, tool='logs', actor=actor, fact=True)
        name = entry['id']+'-logcat.txt'
        (self.w.directory()/name).write_text(json.dumps(recent, ensure_ascii=False, indent=2), encoding='utf-8')
        self.w.event('evidence', '앱 로그 조회 결과', run_id=self.run_id, step_id=entry['id'],
                     attachments=[name], missing=recent['missing'], fact=True)
        return recent

    def interval(self, step_id, cursor):
        result = self.logs.since(cursor)
        name = step_id+'-logcat.txt'
        (self.w.directory()/name).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        self.w.event('evidence', '조작 구간 로그', run_id=self.run_id, step_id=step_id,
                     attachments=[name], missing=result['missing'], fact=True)
        return result

    def close(self):
        self.logs.close()
        exists = (self.w.directory()/self.log_name).is_file()
        self.w.event('evidence', '테스트 지속 로그', run_id=self.run_id,
                     attachments=[self.log_name] if exists else [], missing=list(self.logs.missing), fact=True)

"""플리파: 개인 서버의 사용자별 Android 디버깅·QA 작업실."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import sys
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid
import zipfile
import device
import ai
import emulator
import math
import queue
import struct
try:
    from socketserver import ThreadingUnixStreamServer
except ImportError:
    ThreadingUnixStreamServer = None
from access import AIGate, configured_users
from debug_tools import DebugTools

ROOT = Path(__file__).resolve().parent
if sys.platform == 'win32':
    DEFAULT_DATA = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'Plipa'
else:
    DEFAULT_DATA = Path.home() / 'Library/Application Support/Plipa'
DATA = Path(os.environ.get('PLIPA_DATA', DEFAULT_DATA))


def now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def text(value, limit=5000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('입력 내용을 확인하세요.')
    return device.redact(value.strip())


class Workbench:
    def __init__(self, root=DATA, user='local', devices=None, ai_gate=None):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.stop = threading.Event()
        self.running = False
        self.pending = None
        self.current = None
        self.goal = ''
        self.ai_model = ai.DEFAULT_MODEL
        self.ai_reasoning_effort = ai.DEFAULT_REASONING_EFFORT
        self.viewers = {}
        self.pointer = None
        self.user = user
        self.allowed_devices = devices
        self.ai_gate = ai_gate or AIGate()
        self.steps_left = 0
        self.step_limit = max(1, min(12, int(os.environ.get('PLIPA_AI_STEPS', '6'))))
        self.consent_logs = False
        self.debug = DebugTools(self.session, self.directory, self.event)
        self.managed_avds = set()
        self.last_emulator_activity = time.monotonic()
        self.idle_monitor = None
        self.shutdown = threading.Event()

    def require_device(self, serial):
        if self.allowed_devices is not None and serial not in self.allowed_devices:
            raise ValueError('본인에게 배정된 기기만 사용할 수 있습니다.')

    def emulator_activity(self):
        self.last_emulator_activity = time.monotonic()

    def manage_emulator(self, name):
        with self.lock:
            self.managed_avds.add(name)
            self.emulator_activity()
            if not self.idle_monitor or not self.idle_monitor.is_alive():
                self.idle_monitor = threading.Thread(target=self._idle_emulators, daemon=True)
                self.idle_monitor.start()

    def _idle_emulators(self):
        try:
            idle_seconds = max(60, int(os.environ.get('PLIPA_EMULATOR_IDLE_MINUTES', '20')) * 60)
        except ValueError:
            idle_seconds = 1200
        while not self.shutdown.wait(30):
            with self.lock:
                if not self.managed_avds:
                    return
                if self.viewers or self.running or self.pending:
                    self.emulator_activity()
                    continue
                if time.monotonic() - self.last_emulator_activity < idle_seconds:
                    continue
                managed = set(self.managed_avds)
            stopped = set()
            for entry in device.devices():
                try:
                    name = device.avd_name(entry['serial'])
                    if name in managed:
                        device.stop(entry['serial'])
                        stopped.add(name)
                except ValueError:
                    continue
            with self.lock:
                # Missing names either failed to boot or were already stopped.
                self.managed_avds.difference_update(managed)
                if not self.managed_avds:
                    return

    def close(self):
        self.shutdown.set()

    def session(self):
        if not self.current:
            raise ValueError('테스트를 먼저 시작하거나 이전 기록을 여세요.')
        return self.current

    def directory(self):
        return self.root/self.session()['id']

    def events(self):
        path = self.directory()/'timeline.jsonl'
        if not path.exists():
            return []
        events = []
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                # A crash can interrupt the last append; earlier records stay recoverable.
                continue
        return events

    def event(self, kind, message, **extra):
        with self.lock:
            entry = {'id': uuid.uuid4().hex[:12], 'time': now(), 'kind': kind,
                     'message': device.redact(message), **extra, 'user': self.user}
            with (self.directory()/'timeline.jsonl').open('ab+') as out:
                if out.tell():
                    out.seek(-1, 2)
                    if out.read(1) != b'\n':
                        out.write(b'\n')
                out.write((json.dumps(entry, ensure_ascii=False) + '\n').encode())
                out.flush()
                os.fsync(out.fileno())
            return entry

    def state(self):
        with self.lock:
            sessions = []
            for path in sorted(self.root.glob('*/session.json'), reverse=True):
                try:
                    sessions.append(json.loads(path.read_text(encoding='utf-8')))
                except (ValueError, OSError):
                    continue
            return {'session': self.current, 'sessions': sessions, 'running': self.running,
                    'pending': self.pending, 'events': self.events() if self.current else [],
                    'ai_allowed': self.ai_gate.allowed(self.user),
                    'queue_position': self.ai_gate.position(self.user),
                    'steps_left': self.steps_left, 'step_limit': self.step_limit}

    def start(self, serial, package, title):
        with self.lock:
            self.require_device(serial)
            self.emulator_activity()
            if self.running or self.pending:
                raise ValueError('AI를 중지한 뒤 새 테스트를 시작하세요.')
            if not any(d['serial'] == serial and d['state'] == 'device' for d in device.devices()):
                raise ValueError('연결된 에뮬레이터를 선택하세요.')
            if not isinstance(package, str) or not device.re.fullmatch(r'[a-zA-Z][\w]*(?:\.[\w]+)+', package):
                raise ValueError('앱 패키지 이름을 확인하세요.')
            title = text(title, 120)
            env = device.environment(serial, package)
            sid = datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
            folder = self.root/sid
            folder.mkdir(mode=0o700)
            self.release_pointer()
            self.current = {'id': sid, 'title': title, 'serial': serial,
                            'package': package, 'created': now(), 'environment': env}
            save_json(folder/'session.json', self.current)
            self.event('session', '테스트 시작', fact=True)

    def load(self, sid):
        with self.lock:
            if self.running or self.pending:
                raise ValueError('AI를 중지한 뒤 이전 기록을 여세요.')
            if not isinstance(sid, str) or not device.re.fullmatch(r'[\w-]+', sid):
                raise ValueError('잘못된 기록 ID입니다.')
            path = self.root/sid/'session.json'
            if not path.is_file():
                raise ValueError('기록이 없습니다.')
            restored = json.loads(path.read_text(encoding='utf-8'))
            self.require_device(restored['serial'])
            self.release_pointer()
            self.current = restored

    def capture(self, screen=False):
        s = self.session()
        entry = self.event('capture', '증거 수집', fact=True)
        attachments = []
        missing = []
        for kind in ('ui', 'logs'):
            try:
                self.debug.read(kind, step=entry)
            except ValueError as exc:
                missing.append(str(exc))
        if screen:
            name = entry['id'] + '-screen.png'
            try:
                (self.directory()/name).write_bytes(device.screenshot(s['serial']))
                attachments.append(name)
            except ValueError as exc:
                missing.append(str(exc))
        self.event('evidence', '수집 결과', step_id=entry['id'], attachments=attachments,
                   missing=missing, fact=True)

    def manual(self, action):
        with self.lock:
            if self.running or self.pending:
                raise ValueError('AI를 중지하거나 보류 조작을 취소한 뒤 직접 조작하세요.')
            self.release_pointer()
            self.debug.perform(action)

    def release_pointer(self, viewer=None):
        with self.lock:
            p = self.pointer
            if not p or (viewer is not None and viewer != p['viewer']):
                return
            missing = []
            try:
                p['connection'].mouse(*p['last'], False)
            except ValueError:
                missing.append('연결 오류로 터치 해제 전달 여부 미확인')
                raise
            finally:
                self.pointer = None
                self.event('manual', '수동 터치 종료 요청' if missing else '수동 터치 종료', fact=True, missing=missing,
                           action={'kind': 'gesture', 'points': p['points'],
                                   'coordinates': 'normalized', 'sampled': p['sampled'],
                                   'duration_ms': round((time.monotonic()-p['start'])*1000)})

    def pointer_timeout(self, pointer):
        with self.lock:
            if self.pointer is not pointer:
                return
            if time.monotonic() - self.pointer['updated'] >= 3:
                try:
                    self.release_pointer()
                except ValueError:
                    pass
            else:
                timer = threading.Timer(1, self.pointer_timeout, args=(self.pointer,))
                timer.daemon = True
                timer.start()

    def live_input(self, data):
        with self.lock:
            self.emulator_activity()
            viewer = data.get('viewer')
            if not isinstance(viewer, str) or viewer not in self.viewers:
                raise ValueError('화면 연결을 기다린 뒤 조작하세요.')
            sid, connection = self.viewers[viewer]
            if data.get('session') != self.session()['id'] or sid != self.session()['id']:
                raise ValueError('테스트가 바뀌었습니다. 화면을 다시 연결하세요.')
            phase = data.get('phase')
            if phase == 'cancel':
                self.release_pointer(viewer)
                return
            if self.running or self.pending:
                raise ValueError('AI를 중지하거나 보류 조작을 취소한 뒤 직접 조작하세요.')
            if phase == 'key':
                key = data.get('key')
                if not isinstance(key, str) or not (key in ('GoBack', 'GoHome', 'Enter', 'Backspace',
                    'Tab', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Escape') or
                    len(key) == 1 and 32 <= ord(key) <= 126):
                    raise ValueError('지원하지 않는 키입니다. 한글은 에뮬레이터 키보드를 사용하세요.')
                if self.pointer:
                    raise ValueError('터치를 끝낸 뒤 키를 입력하세요.')
                connection.key(key)
                self.event('manual', '수동 키 입력', action={'kind': 'key',
                           'text': key if len(key) > 1 else '[입력 내용 기록 안 함]'}, fact=True)
                return
            if phase not in ('down', 'move', 'up'):
                raise ValueError('잘못된 터치 요청입니다.')
            coords = [data.get('x'), data.get('y')]
            if any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in coords):
                raise ValueError('잘못된 화면 좌표입니다.')
            if phase == 'down':
                if self.pointer:
                    raise ValueError('이미 진행 중인 터치가 있습니다.')
                self.pointer = {'viewer': viewer, 'connection': connection, 'last': coords,
                                'start': time.monotonic(), 'updated': time.monotonic(),
                                'points': [coords], 'sampled': False}
                timer = threading.Timer(1, self.pointer_timeout, args=(self.pointer,))
                timer.daemon = True
                timer.start()
            p = self.pointer
            if not p or p['viewer'] != viewer:
                raise ValueError('진행 중인 터치가 아닙니다.')
            p['last'] = coords
            p['updated'] = time.monotonic()
            if coords != p['points'][-1]:
                if len(p['points']) >= 120:
                    p['points'] = p['points'][::2]
                    p['sampled'] = True
                p['points'].append(coords)
            if phase == 'up':
                self.release_pointer(viewer)
            else:
                try:
                    connection.mouse(*coords, True)
                except ValueError:
                    self.release_pointer(viewer)
                    raise

    def start_ai(self, goal, consent, consent_logs=False, model=None, reasoning_effort=None):
        with self.lock:
            self.session()
            if consent is not True:
                raise ValueError('AI에 현재 화면 텍스트를 전송하는 데 동의해야 합니다.')
            if self.running or self.pending:
                raise ValueError('진행 중인 AI 테스트가 있습니다.')
            if not self.ai_gate.allowed(self.user):
                raise ValueError('소유자가 AI 사용을 제한했습니다.')
            self.release_pointer()
            self.goal = text(goal, 3000)
            self.ai_model, self.ai_reasoning_effort = ai.settings(model, reasoning_effort)
            self.steps_left = self.step_limit
            self.consent_logs = consent_logs is True
            self.event('user', self.goal, external_ai_consent=True, logs_ai_consent=self.consent_logs,
                       ai_model=self.ai_model, ai_reasoning_effort=self.ai_reasoning_effort)
            self._spawn()

    def _spawn(self):
        self.stop.clear()
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        try:
            with self.ai_gate.slot(self.user, self.stop):
                self._run_ai()
        except Exception as exc:
            self.event('error', str(exc) if isinstance(exc, ValueError) else 'AI 실행 중 오류가 발생했습니다. 기록은 보존했습니다.')
        finally:
            with self.lock:
                self.running = False

    def _run_ai(self):
        evidence = None
        while self.steps_left > 0:
            if self.stop.is_set():
                return
            with self.operation:
                nodes = self.debug.read('ui', actor='ai')
            history = [{'kind': e['kind'], 'message': e['message'], 'action': e.get('action')}
                       for e in self.events() if e['kind'] in ('user', 'ai', 'action', 'error')]
            if self.stop.is_set():
                return
            self.steps_left -= 1
            action = ai.plan(self.goal, nodes, history, self.stop, evidence=evidence,
                             logs_allowed=self.consent_logs, model=self.ai_model,
                             reasoning_effort=self.ai_reasoning_effort)
            with self.lock:
                if self.stop.is_set():
                    return
                self.event('ai', action['message'], action=action, fact=False)
                if action['kind'] == 'done':
                    return
                if action['kind'] not in ('logs', 'environment') and not device.auto_allowed(action):
                    self.pending = {'id': uuid.uuid4().hex, 'action': action, 'nodes': nodes, 'created': now()}
                    return
            if action['kind'] in ('logs', 'environment'):
                if action['kind'] == 'logs' and not self.consent_logs:
                    self.event('system', '앱 로그 AI 전송 동의가 없어 중지했습니다. 동의 후 다시 요청하세요.')
                    return
                with self.operation:
                    if self.stop.is_set():
                        return
                    evidence = {'tool': action['kind'], 'result': self.debug.read(action['kind'], actor='ai')}
                continue
            # observe is a request for another observation, never an executed input.
            if self.stop.wait(1):
                return
        self.event('system', '요청의 AI 호출 한도에 도달했습니다. 기록을 확인한 뒤 다시 지시하세요.')

    def approve(self, pending_id, approved):
        with self.operation, self.lock:
            pending = self.pending
            if not pending or pending_id != pending['id'] or self.running:
                raise ValueError('현재 승인 대기 조작이 아닙니다.')
            if approved is True and not self.ai_gate.allowed(self.user):
                raise ValueError('소유자가 AI 사용을 제한했습니다.')
            self.pending = None
            if approved is not True:
                self.event('system', '사용자가 조작을 취소했습니다.')
                return
            fresh = device.tree(self.session()['serial'])
            if fresh != pending['nodes']:
                self.event('system', '화면이 변경되어 승인을 폐기했습니다.')
                raise ValueError('화면이 달라졌습니다. AI에 다시 지시하세요.')
            action = pending['action']
            self.debug.perform(action, fresh, actor='ai', approved=True)
            if self.steps_left > 0:
                self._spawn()
            else:
                self.event('system', '승인한 조작을 실행했습니다. AI 호출 한도에 도달해 추가 판단은 실행하지 않았습니다.')

    def cancel(self):
        with self.lock:
            self.stop.set()
            self.pending = None
            if self.current:
                self.event('system', 'AI 중지 요청. 실행 전인 조작을 취소했습니다.')

    def export(self, data):
        with self.lock:
            events = self.events()
            ids = data.get('ids')
            if not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids):
                raise ValueError('공유할 기록을 하나 이상 선택하세요.')
            selected = [e for e in events if e['id'] in ids]
            if len(selected) != len(set(ids)):
                raise ValueError('존재하지 않는 기록이 선택됐습니다.')
            linked = {e['step_id'] for e in selected if e.get('step_id')}
            selected = [e for e in events if e['id'] in ids or e['id'] in linked]
            report = {'title': text(data.get('title'), 120), 'steps': text(data.get('steps')),
                      'expected': text(data.get('expected')), 'actual': text(data.get('actual'))}
            bundle = io.BytesIO()
            files = []
            with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as z:
                exported = []
                for event in selected:
                    event = dict(event)
                    event['attachments'] = []
                    for name in next(e for e in selected if e['id'] == event['id']).get('attachments', []):
                        category = 'screens' if name.endswith('.png') else 'logs' if name.endswith('.txt') else 'ui'
                        if data.get(category) is not True:
                            continue
                        path = self.directory()/name
                        if path.is_file():
                            z.write(path, 'attachments/' + name)
                            event['attachments'].append('attachments/' + name)
                            files.append('attachments/' + name)
                    exported.append(event)
                z.writestr('report.md', f"# {report['title']}\n\n## 재현 순서\n\n{report['steps']}\n\n## 기대 결과\n\n{report['expected']}\n\n## 실제 결과\n\n{report['actual']}\n\n사용자가 작성한 보고서. AI 추정은 timeline.jsonl의 fact=false 항목을 참고하세요.\n")
                z.writestr('timeline.jsonl', ''.join(json.dumps(e, ensure_ascii=False)+'\n' for e in exported))
                z.writestr('manifest.json', json.dumps({'format': 'plipa.qa.v1', 'created': now(),
                    'environment': self.session()['environment'], 'event_ids': [e['id'] for e in selected], 'attachments': files,
                    'missing': ['네트워크 본문, 녹화, 성능 트레이스, 앱 내부 상태는 첫 버전 미수집',
                                '로그캣은 수집 버튼 시점의 앱 PID 최근 200줄 표본',
                                '플리파 외부에서 한 조작은 타임라인에 자동 기록되지 않음'],
                    'sharing': '사용자가 선택한 동료 공유용 파일. 외부 AI 전송 허가와 별개.'}, ensure_ascii=False, indent=2))
            return bundle.getvalue()


class Workspaces:
    def __init__(self, root, users):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.users = users
        self.gate = AIGate(root/'ai-access.json')
        self.boards = {name: Workbench(root if spec['role'] == 'owner' else root/'users'/name,
                       user=name, devices=spec['devices'], ai_gate=self.gate)
                       for name, spec in users.items()}
        self.csrf = {name: secrets.token_urlsafe(32) for name in users}

    def members(self):
        return [{'id': name, 'ai_allowed': self.gate.allowed(name), 'running': w.running,
                 'pending': bool(w.pending), 'queue_position': self.gate.position(name)}
                for name, w in self.boards.items()]

    def control(self, actor, target, allowed):
        if self.users[actor]['role'] != 'owner':
            raise ValueError('소유자만 AI 사용을 관리할 수 있습니다.')
        if target not in self.users or type(allowed) is not bool:
            raise ValueError('사용자와 AI 허용 여부를 확인하세요.')
        w = self.boards[target]
        with w.lock:
            self.gate.set_allowed(target, allowed)
            if not allowed:
                w.cancel()


if ThreadingUnixStreamServer is not None:
    class PrivateHTTPServer(ThreadingUnixStreamServer):
        daemon_threads = True
        supported = True
else:
    class PrivateHTTPServer:
        supported = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Request data may contain QA evidence. Never log it.

    def reply(self, value, status=200, mime='application/json; charset=utf-8', download=False):
        body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'")
        if download:
            self.send_header('Content-Disposition', 'attachment; filename="plipa-qa.zip"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def trusted(self):
        origin = (getattr(self.server, 'origin', None)
                  or getattr(self.server, 'public_origin', None)
                  or f'http://127.0.0.1:{self.server.server_port}')
        return self.headers.get('Host') == urlsplit(origin).netloc and self.headers.get('Origin', origin) == origin

    def identify(self):
        registry = getattr(self.server, 'workspaces', None)
        if registry is None:  # Isolated test harness and legacy loopback server.
            self.workbench = self.server.workbench
            self.csrf = self.server.csrf
            self.user = 'local'
            self.role = 'owner'
            return True
        # Shared mode accepts identity only through a 0600 Unix socket behind an authenticated proxy.
        self.user = self.headers.get('X-Plipa-User') if isinstance(self.server, PrivateHTTPServer) else 'local'
        if self.user not in registry.users:
            self.reply({'error': '인증된 사용자로 접속하세요.'}, 403)
            return False
        self.role = registry.users[self.user]['role']
        self.workbench = registry.boards[self.user]
        self.csrf = registry.csrf[self.user]
        return True

    def do_GET(self):
        if not self.trusted():
            return self.reply({'error': '등록한 주소로 접속하세요.'}, 403)
        if not self.identify():
            return
        path = urlsplit(self.path).path
        try:
            if path == '/api/state':
                registry = getattr(self.server, 'workspaces', None)
                return self.reply({**self.workbench.state(), 'csrf': self.csrf,
                    'user': self.user, 'role': self.role,
                    'members': registry.members() if registry and self.role == 'owner' else []})
            if path == '/api/devices':
                allowed = self.workbench.allowed_devices
                return self.reply({'devices': [d for d in device.devices() if allowed is None or d['serial'] in allowed],
                                   'avds': device.avds() if self.role == 'owner' else []})
            if path == '/api/screen':
                with self.workbench.operation:
                    return self.reply(device.screenshot(self.workbench.session()['serial']), mime='image/png')
            if path == '/api/attachment':
                w = self.workbench
                name = parse_qs(urlsplit(self.path).query).get('name', [''])[0]
                with w.lock:
                    allowed = {n for e in w.events() for n in e.get('attachments', [])}
                    if name not in allowed or Path(name).name != name:
                        return self.reply({'error': '선택한 기록의 자료가 아닙니다.'}, 404)
                    attachment = w.directory()/name
                    if not attachment.is_file():
                        return self.reply({'error': '자료 파일이 없습니다. 원본 기록은 유지됩니다.'}, 404)
                    payload = attachment.read_bytes()
                return self.reply(payload, mime='image/png' if name.endswith('.png') else 'text/plain; charset=utf-8')
            assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript'), '/stream.js': ('stream.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
            if path in assets:
                name, mime = assets[path]
                return self.reply((ROOT/'dist'/name).read_bytes(), mime=mime)
            self.reply({'error': '찾을 수 없습니다.'}, 404)
        except ValueError as exc:
            self.reply({'error': str(exc)}, 400)

    def stream(self, data):
        w = self.workbench
        viewer = data.get('viewer')
        if not isinstance(viewer, str) or not device.re.fullmatch(r'[a-zA-Z0-9-]{1,64}', viewer):
            raise ValueError('잘못된 화면 연결 ID입니다.')
        with w.lock:
            w.emulator_activity()
            sid = w.session()['id']
            if data.get('session') != sid or viewer in w.viewers:
                raise ValueError('화면 연결을 다시 시작하세요.')
            connection = emulator.Connection(w.session()['serial'])
            w.viewers[viewer] = (sid, connection)
        frames = connection.frames()
        latest = queue.Queue(maxsize=1)
        ended = threading.Event()

        def receive():
            try:
                for frame in frames:
                    width, height = connection.size
                    if (frame.format.width > frame.format.height) != (width > height):
                        connection.size = (height, width)
                    try:
                        latest.get_nowait()
                    except queue.Empty:
                        pass
                    latest.put_nowait(frame.image)
            except Exception:
                pass  # Never expose emulator authentication metadata.
            finally:
                ended.set()

        worker = threading.Thread(target=receive, daemon=True)
        worker.start()
        try:
            self.connection.settimeout(3)
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            while not ended.is_set():
                with w.lock:
                    if w.session()['id'] != sid:
                        break
                try:
                    frame = latest.get(timeout=1)
                except queue.Empty:
                    frame = b''  # Idle screens have no frames; heartbeat detects closed viewers.
                self.wfile.write(struct.pack('!I', len(frame)) + frame)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            frames.cancel()
            with w.lock:
                try:
                    w.release_pointer(viewer)
                except ValueError:
                    pass
                w.viewers.pop(viewer, None)
            connection.close()
            worker.join(timeout=3)

    def do_POST(self):
        if not self.trusted():
            return self.reply({'error': '접근이 거부됐습니다. 화면을 새로고침하세요.'}, 403)
        if not self.identify():
            return
        if not secrets.compare_digest(self.headers.get('X-Plipa-CSRF', ''), self.csrf):
            return self.reply({'error': '접근이 거부됐습니다. 화면을 새로고침하세요.'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 64000 or not self.headers.get('Content-Type', '').startswith('application/json'):
                raise ValueError('요청 형식 또는 크기가 올바르지 않습니다.')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('JSON 객체가 필요합니다.')
            w = self.workbench
            path = urlsplit(self.path).path
            if path == '/api/admin/ai':
                self.server.workspaces.control(self.user, data.get('user'), data.get('allowed'))
            elif path == '/api/stream':
                return self.stream(data)
            elif path == '/api/input':
                w.live_input(data)
            elif path == '/api/approve':
                w.approve(data.get('id'), data.get('approved'))
            elif path == '/api/stop':
                w.cancel()
            else:
                with w.operation:
                    if path == '/api/start':
                        w.start(data.get('serial'), data.get('package'), data.get('title'))
                    elif path == '/api/load':
                        w.load(data.get('id'))
                    elif path == '/api/boot':
                        if self.role != 'owner':
                            raise ValueError('소유자가 에뮬레이터를 준비해야 합니다.')
                        w.manage_emulator(device.boot(data.get('name')))
                    elif path == '/api/launch':
                        if w.running or w.pending:
                            raise ValueError('AI를 중지한 뒤 앱을 실행하세요.')
                        with w.lock:
                            w.release_pointer()
                            w.debug.launch()
                    elif path == '/api/manual':
                        w.manual(data)
                    elif path == '/api/capture':
                        w.capture(data.get('screen') is True)
                    elif path == '/api/debug':
                        return self.reply({'result': w.debug.read(data.get('tool'))})
                    elif path == '/api/chat':
                        w.start_ai(data.get('goal'), data.get('consent'), data.get('consent_logs'),
                                   data.get('model'), data.get('reasoning_effort'))
                    elif path == '/api/note':
                        w.event('note', text(data.get('message')), fact=False)
                    elif path == '/api/export':
                        return self.reply(w.export(data), mime='application/zip', download=True)
                    else:
                        return self.reply({'error': '찾을 수 없습니다.'}, 404)
            self.reply({'ok': True})
        except (ValueError, TypeError, KeyError) as exc:
            self.reply({'error': device.redact(str(exc))}, 400)
        except Exception:
            self.reply({'error': '처리하지 못했습니다. 기존 기록은 보존했습니다.'}, 500)


def server_configuration(port):
    users = configured_users()
    socket_path = os.environ.get('PLIPA_SOCKET')
    origin = os.environ.get('PLIPA_ORIGIN', f'http://127.0.0.1:{port}').rstrip('/')
    parsed = urlsplit(origin)
    if (parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.path
            or parsed.query or parsed.fragment or parsed.username or parsed.password):
        raise ValueError('PLIPA_ORIGIN은 경로가 없는 http 또는 https 주소여야 합니다.')
    shared = os.environ.get('PLIPA_USERS') is not None
    if shared:
        if not socket_path or parsed.scheme != 'https':
            raise ValueError('공동 사용은 PLIPA_SOCKET과 HTTPS PLIPA_ORIGIN, 인증 프록시가 필요합니다.')
        if not PrivateHTTPServer.supported:
            raise ValueError('이 운영체제에서는 인증 프록시용 Unix 소켓 공동 사용을 지원하지 않습니다.')
    elif socket_path:
        raise ValueError('PLIPA_SOCKET은 공동 사용 설정에서만 사용하세요.')
    return users, socket_path, origin, shared


def main():
    os.umask(0o077)
    port = int(os.environ.get('PLIPA_PORT', '4317'))
    users, socket_path, origin, shared = server_configuration(port)
    if shared:
        server = PrivateHTTPServer(socket_path, Handler)
        os.chmod(socket_path, 0o600)
    else:
        server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.origin = origin
    server.public_origin = origin
    server.workspaces = Workspaces(DATA, users)
    print(f'플리파 {origin}', flush=True)
    import sys
    if '--open' in sys.argv and not socket_path:
        import webbrowser
        threading.Timer(.3, lambda: webbrowser.open(origin)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        for w in server.workspaces.boards.values():
            w.cancel()
    finally:
        try:
            for w in server.workspaces.boards.values():
                w.release_pointer()
        finally:
            for w in server.workspaces.boards.values():
                w.close()
            server.server_close()
            if socket_path:
                Path(socket_path).unlink(missing_ok=True)


if __name__ == '__main__':
    main()

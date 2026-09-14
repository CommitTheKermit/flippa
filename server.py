"""플리파: single-user loopback QA workbench, Python standard library only."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import parse_qs, urlsplit
import uuid
import zipfile
import device
import ai

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('PLIPA_DATA', Path.home() / 'Library/Application Support/Plipa'))


def now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    tmp.replace(path)


def text(value, limit=5000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError('입력 내용을 확인하세요.')
    return device.redact(value.strip())


class Workbench:
    def __init__(self, root=DATA):
        self.root = root
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.operation = threading.Lock()
        self.stop = threading.Event()
        self.running = False
        self.pending = None
        self.current = None
        self.goal = ''

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
        for line in path.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                # A crash can interrupt the last append; earlier records stay recoverable.
                continue
        return events

    def event(self, kind, message, **extra):
        with self.lock:
            entry = {'id': uuid.uuid4().hex[:12], 'time': now(), 'kind': kind,
                     'message': device.redact(message), **extra}
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
                    sessions.append(json.loads(path.read_text()))
                except (ValueError, OSError):
                    continue
            return {'session': self.current, 'sessions': sessions, 'running': self.running,
                    'pending': self.pending, 'events': self.events() if self.current else []}

    def start(self, serial, package, title):
        with self.lock:
            if self.running or self.pending:
                raise ValueError('AI를 중지한 뒤 새 테스트를 시작하세요.')
            if not any(d['serial'] == serial and d['state'] == 'device' for d in device.devices()):
                raise ValueError('연결된 에뮬레이터를 선택하세요.')
            if not isinstance(package, str) or not device.re.fullmatch(r'[a-zA-Z][\w]*(?:\.[\w]+)+', package):
                raise ValueError('앱 패키지 이름을 확인하세요.')
            env = device.environment(serial, package)
            sid = datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
            folder = self.root/sid
            folder.mkdir(mode=0o700)
            self.current = {'id': sid, 'title': text(title, 120), 'serial': serial,
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
            self.current = json.loads(path.read_text())

    def capture(self, screen=False):
        s = self.session()
        entry = self.event('capture', '증거 수집', fact=True)
        attachments = []
        missing = []
        for suffix, collect in [('ui.json', lambda: json.dumps(device.tree(s['serial']), ensure_ascii=False, indent=2).encode()),
                                ('logcat.txt', lambda: device.logs(s['serial'], s['package']).encode())]:
            name = entry['id'] + '-' + suffix
            try:
                (self.directory()/name).write_bytes(collect())
                attachments.append(name)
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
            device.perform(self.session()['serial'], action)
            safe_action = dict(action)
            if action.get('kind') == 'text':
                safe_action['text'] = '[입력 내용 기록 안 함]'
            self.event('manual', '수동 조작', action=safe_action, fact=True)

    def start_ai(self, goal, consent):
        with self.lock:
            self.session()
            if consent is not True:
                raise ValueError('AI에 현재 화면 텍스트를 전송하는 데 동의해야 합니다.')
            if self.running or self.pending:
                raise ValueError('진행 중인 AI 테스트가 있습니다.')
            self.goal = text(goal, 3000)
            self.event('user', self.goal, external_ai_consent=True)
            self._spawn()

    def _spawn(self):
        self.stop.clear()
        self.running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        try:
            for _ in range(12):
                if self.stop.is_set():
                    break
                with self.operation:
                    nodes = device.tree(self.session()['serial'])
                history = [{'kind': e['kind'], 'message': e['message'], 'action': e.get('action')}
                           for e in self.events() if e['kind'] in ('user', 'ai', 'action', 'error')]
                action = ai.plan(self.goal, nodes, history, self.stop)
                with self.lock:
                    if self.stop.is_set():
                        break
                    self.event('ai', action['message'], action=action, fact=False)
                    if action['kind'] == 'done':
                        break
                    if not device.auto_allowed(action):
                        self.pending = {'id': uuid.uuid4().hex, 'action': action, 'nodes': nodes, 'created': now()}
                        break
                with self.operation:
                    with self.lock:
                        if self.stop.is_set():
                            break
                        fresh = device.tree(self.session()['serial'])
                        if action['kind'] == 'tap' and fresh != nodes:
                            raise ValueError('화면이 바뀌어 자동 탐색을 중단했습니다. 다시 지시하세요.')
                        device.perform(self.session()['serial'], action, fresh)
                        self.event('action', 'AI 조작 실행', action=action, fact=True)
                if self.stop.wait(1):
                    break
            else:
                self.event('system', '12단계에 도달해 멈췄습니다. 화면 확인 후 다음 지시를 입력하세요.')
        except Exception as exc:
            self.event('error', str(exc) if isinstance(exc, ValueError) else 'AI 실행 중 오류가 발생했습니다. 기록은 보존했습니다.')
        finally:
            with self.lock:
                self.running = False

    def approve(self, pending_id, approved):
        with self.operation, self.lock:
            pending = self.pending
            if not pending or pending_id != pending['id'] or self.running:
                raise ValueError('현재 승인 대기 조작이 아닙니다.')
            self.pending = None
            if approved is not True:
                self.event('system', '사용자가 조작을 취소했습니다.')
                return
            fresh = device.tree(self.session()['serial'])
            if fresh != pending['nodes']:
                self.event('system', '화면이 변경되어 승인을 폐기했습니다.')
                raise ValueError('화면이 달라졌습니다. AI에 다시 지시하세요.')
            action = pending['action']
            device.perform(self.session()['serial'], action, fresh)
            self.event('action', '사용자 승인 후 실행', action=action, approved=True, fact=True)
            self._spawn()

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
        host = f'127.0.0.1:{self.server.server_port}'
        return self.headers.get('Host') == host and self.headers.get('Origin', 'http://' + host) == 'http://' + host

    def do_GET(self):
        if not self.trusted():
            return self.reply({'error': '로컬 접속만 허용합니다.'}, 403)
        path = urlsplit(self.path).path
        try:
            if path == '/api/state':
                return self.reply({**self.server.workbench.state(), 'csrf': self.server.csrf})
            if path == '/api/devices':
                return self.reply({'devices': device.devices(), 'avds': device.avds()})
            if path == '/api/screen':
                return self.reply(device.screenshot(self.server.workbench.session()['serial']), mime='image/png')
            if path == '/api/attachment':
                w = self.server.workbench
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
            assets = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
            if path in assets:
                name, mime = assets[path]
                return self.reply((ROOT/'dist'/name).read_bytes(), mime=mime)
            self.reply({'error': '찾을 수 없습니다.'}, 404)
        except ValueError as exc:
            self.reply({'error': str(exc)}, 400)

    def do_POST(self):
        if not self.trusted() or not secrets.compare_digest(self.headers.get('X-Plipa-CSRF', ''), self.server.csrf):
            return self.reply({'error': '접근이 거부됐습니다. 화면을 새로고침하세요.'}, 403)
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 64000 or not self.headers.get('Content-Type', '').startswith('application/json'):
                raise ValueError('요청 형식 또는 크기가 올바르지 않습니다.')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError('JSON 객체가 필요합니다.')
            w = self.server.workbench
            path = urlsplit(self.path).path
            # ponytail: one local user, serialize device operations; per-device queues for multiple users.
            if path == '/api/approve':
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
                        device.boot(data.get('name'))
                    elif path == '/api/launch':
                        if w.running or w.pending:
                            raise ValueError('AI를 중지한 뒤 앱을 실행하세요.')
                        device.launch(w.session()['serial'], w.session()['package'])
                        w.event('manual', '대상 앱 실행', fact=True)
                    elif path == '/api/manual':
                        w.manual(data)
                    elif path == '/api/capture':
                        w.capture(data.get('screen') is True)
                    elif path == '/api/chat':
                        w.start_ai(data.get('goal'), data.get('consent'))
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


def main():
    os.umask(0o077)
    port = int(os.environ.get('PLIPA_PORT', '4317'))
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.csrf = secrets.token_urlsafe(32)
    server.workbench = Workbench()
    print(f'플리파 http://127.0.0.1:{port}', flush=True)
    import sys
    if '--open' in sys.argv:
        import webbrowser
        threading.Timer(.3, lambda: webbrowser.open(f'http://127.0.0.1:{port}')).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.workbench.cancel()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()

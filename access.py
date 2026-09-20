"""개인 서버의 사용자 배정과 Codex 실행 순서·사용 제어."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import threading


def configured_users():
    raw = os.environ.get('PLIPA_USERS')
    if raw is None:
        return {'local': {'role': 'owner', 'devices': None}}
    try:
        users = json.loads(raw)
        if not isinstance(users, dict) or not users:
            raise ValueError()
        assigned = set()
        for name, user in users.items():
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', name) or not isinstance(user, dict):
                raise ValueError()
            if user.get('role') not in ('owner', 'tester'):
                raise ValueError()
            devices = user.get('devices')
            if (not isinstance(devices, list) or not devices or
                any(not isinstance(d, str) or not re.fullmatch(r'emulator-\d+', d) for d in devices) or
                len(set(devices)) != len(devices) or assigned.intersection(devices)):
                raise ValueError()
            assigned.update(devices)
        if sum(u['role'] == 'owner' for u in users.values()) != 1:
            raise ValueError()
        return users
    except (ValueError, TypeError, AttributeError):
        raise ValueError('PLIPA_USERS에 소유자 1명과 접속자별 서로 다른 기기 목록을 설정하세요.') from None


class AIGate:
    """모든 사용자가 하나의 Codex 실행 슬롯을 FIFO로 공유한다."""
    def __init__(self, path=None):
        self.condition = threading.Condition()
        self.waiting = []
        self.active = None
        self.path = Path(path) if path else None
        self.disabled = set()
        if self.path and self.path.exists():
            self.disabled = set(json.loads(self.path.read_text()))

    def allowed(self, user):
        with self.condition:
            return user not in self.disabled

    def set_allowed(self, user, allowed):
        with self.condition:
            updated = self.disabled - {user} if allowed else self.disabled | {user}
            if self.path:
                tmp = self.path.with_suffix('.tmp')
                tmp.write_text(json.dumps(sorted(updated)))
                tmp.replace(self.path)
            self.disabled = updated
            self.condition.notify_all()

    def position(self, user):
        with self.condition:
            return self.waiting.index(user) + 1 if user in self.waiting else 0

    @contextmanager
    def slot(self, user, stop):
        with self.condition:
            if user in self.disabled or stop.is_set():
                raise ValueError('AI 실행이 중지됐거나 소유자가 사용을 제한했습니다.')
            self.waiting.append(user)
            try:
                while self.active is not None or self.waiting[0] != user:
                    if stop.is_set() or user in self.disabled:
                        raise ValueError('대기 중인 AI 요청이 취소됐습니다.')
                    self.condition.wait(.2)
                if stop.is_set() or user in self.disabled:
                    raise ValueError('AI 요청이 취소됐습니다.')
                self.waiting.pop(0)
                self.active = user
            except BaseException:
                self.waiting.remove(user)
                self.condition.notify_all()
                raise
        try:
            yield
        finally:
            with self.condition:
                self.active = None
                self.condition.notify_all()

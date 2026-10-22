"""Durable remote validation requests and host-wide resource ownership."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid


class ResourceBusy(ValueError):
    pass


@contextmanager
def resource_lock(path):
    """One cross-process lock, supported on Windows and POSIX."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b'0'); handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ResourceBusy('다른 작업이 빌드 또는 기기를 사용 중입니다.') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_config(path):
    config = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    for key in ('state_dir', 'app_repo', 'origin', 'profiles', 'signers', 'package', 'avd'):
        if not config.get(key):
            raise ValueError('원격 검증 설정 누락: '+key)
    for key in ('state_dir', 'app_repo'):
        if not Path(config[key]).is_absolute():
            raise ValueError('절대 경로가 필요합니다: '+key)
    return config


class JobStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS jobs (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, request_id TEXT UNIQUE, request TEXT, state TEXT, phase TEXT, created REAL, updated REAL, result TEXT)')

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root/'jobs.sqlite3', timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def submit(self, request, profiles):
        if set(request) != {'request_id', 'sha', 'profile'}:
            raise ValueError('request_id, sha, profile만 제출할 수 있습니다.')
        if not re.fullmatch(r'[a-f0-9]{40}', request['sha']):
            raise ValueError('40자리 커밋 SHA가 필요합니다.')
        if not re.fullmatch(r'[a-zA-Z0-9_-]{8,100}', request['request_id']):
            raise ValueError('유효한 request_id가 필요합니다.')
        if request['profile'] not in profiles:
            raise ValueError('허용되지 않은 검증 종류입니다.')
        encoded = json.dumps(request, sort_keys=True)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM jobs WHERE request_id=?', (request['request_id'],)).fetchone()
            if old:
                if old['request'] != encoded:
                    raise ValueError('동일 request_id의 요청 내용을 변경할 수 없습니다.')
                return self._decode(old)
            job = uuid.uuid4().hex
            now = time.time()
            db.execute('INSERT INTO jobs(id,request_id,request,state,phase,created,updated) VALUES (?,?,?,?,?,?,?)', (job,request['request_id'],encoded,'queued','queued',now,now))
        return self.get(job)

    @staticmethod
    def _decode(row):
        data = dict(row)
        data['request'] = json.loads(data['request'])
        data['result'] = json.loads(data['result']) if data['result'] else None
        return data

    def get(self, job):
        with self.db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (job,)).fetchone()
        if not row:
            raise ValueError('작업을 찾지 못했습니다.')
        return self._decode(row)

    def list(self):
        with self.db() as db:
            return [self._decode(row) for row in db.execute('SELECT * FROM jobs ORDER BY seq DESC LIMIT 50')]

    def claim(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            # Crash recovery is explicit; never rerun installation based on elapsed time.
            if db.execute("SELECT 1 FROM jobs WHERE state IN ('running','cancelling','blocked')").fetchone():
                return None
            row = db.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY seq LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET state='running',phase='starting',updated=? WHERE id=?", (time.time(),row['id']))
            return row['id']

    def phase(self, job, phase):
        with self.db() as db:
            db.execute('UPDATE jobs SET phase=?,updated=? WHERE id=?', (phase,time.time(),job))

    def finish(self, job, state, result):
        with self.db() as db:
            db.execute('UPDATE jobs SET state=?,phase=?,result=?,updated=? WHERE id=?', (state,state,json.dumps(result,ensure_ascii=False),time.time(),job))

    def cancel(self, job):
        self.get(job)
        with self.db() as db:
            db.execute("UPDATE jobs SET state=CASE WHEN state='queued' THEN 'cancelled' WHEN state='running' THEN 'cancelling' ELSE state END,updated=? WHERE id=?", (time.time(),job))
        return self.get(job)

    def cancelled(self, job):
        return self.get(job)['state'] == 'cancelling'

    def recover(self, job):
        # Caller must inspect orphan commands and devices, then hold the worker lock.
        row = self.get(job)
        if row['state'] not in ('running','cancelling','blocked'):
            raise ValueError('복구 대상 상태가 아닙니다.')
        self.finish(job,'failed',{'error':'실행 중단 후 운영자가 프로세스와 기기를 확인하고 복구했습니다. 자동 재실행하지 않았습니다.'})
        return self.get(job)


def sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

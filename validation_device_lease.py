"""Conservative, Windows-compatible records for the existing AVD lease registry."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

from validation_jobs import ResourceBusy


class DeviceLease:
    def __init__(self, avd, owner, directory=None):
        if not re.fullmatch(r'[A-Za-z0-9._-]+', avd):
            raise ValueError('유효하지 않은 AVD 이름입니다.')
        self.path = Path(directory or Path.home()/'.codex/android-emulator-leases')/(avd+'.json')
        now = time.time()
        stamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        self.record = dict(version=1, avd=avd, owner=owner, serial=None,
                           created_epoch=now, updated_epoch=now, created_at=stamp, updated_at=stamp)

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.path.open('x', encoding='utf-8') as output:
                json.dump(self.record, output)
        except FileExistsError:
            raise ResourceBusy('기존 AVD 임대가 있어 자동 인수하지 않습니다.') from None

    def bind(self, serial):
        if json.loads(self.path.read_text())['owner'] != self.record['owner']:
            raise ResourceBusy('AVD 임대 소유자가 변경됐습니다.')
        self.record.update(serial=serial, updated_epoch=time.time())
        self.path.write_text(json.dumps(self.record), encoding='utf-8')

    def release(self):
        if json.loads(self.path.read_text())['owner'] != self.record['owner']:
            raise ResourceBusy('AVD 임대 소유자가 변경됐습니다.')
        self.path.unlink()

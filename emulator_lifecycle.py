"""Default Android emulator preparation shared by web requests."""
import os
import threading
import time

import device


class EmulatorLifecycle:
    """Prepare one configured AVD once and expose progress for the web UI."""

    def __init__(self, default_name=None, boot_timeout=180):
        self.configured_default = default_name or os.environ.get('PLIPA_DEFAULT_AVD', '')
        self.boot_timeout = boot_timeout
        self.lock = threading.RLock()
        self.phase = 'idle'
        self.target_name = ''
        self.serial = ''
        self.message = '에뮬레이터 준비를 기다리고 있습니다.'
        self.worker = None

    def _default_name(self, available):
        if self.configured_default:
            return self.configured_default
        return available[0] if len(available) == 1 else ''

    def status(self):
        available = device.avds()
        default_name = self._default_name(available)
        with self.lock:
            target = self.target_name or default_name
            if target:
                running = device.find_avd(target)
                if running and running['state'] == 'device' and device.boot_completed(running['serial']):
                    self.phase = 'ready'
                    self.serial = running['serial']
                    self.message = '에뮬레이터가 준비됐습니다.'
            if not default_name:
                message = ('기본 에뮬레이터가 설정되지 않았습니다.' if available
                           else '등록된 에뮬레이터가 없습니다.')
                if self.phase != 'starting':
                    self.phase = 'unavailable'
                    self.message = message
            elif default_name not in available and self.phase != 'starting':
                self.phase = 'unavailable'
                self.message = '설정한 기본 에뮬레이터를 찾지 못했습니다.'
            return {'phase': self.phase, 'name': target or default_name,
                    'serial': self.serial, 'message': self.message,
                    'default': default_name}

    def prepare_default(self):
        return self.prepare(None)

    def prepare(self, requested_name):
        available = device.avds()
        name = requested_name or self._default_name(available)
        if not name:
            raise ValueError('기본 에뮬레이터를 설정하거나 하나를 선택하세요.')
        if name not in available:
            raise ValueError('등록된 가상 기기를 선택하세요.')
        with self.lock:
            running = device.find_avd(name)
            if running and running['state'] == 'device' and device.boot_completed(running['serial']):
                self.phase = 'ready'
                self.target_name = name
                self.serial = running['serial']
                self.message = '에뮬레이터가 준비됐습니다.'
                return self.status()
            if self.worker and self.worker.is_alive():
                if self.target_name != name:
                    raise ValueError('다른 에뮬레이터를 준비하고 있습니다.')
                return self.status()
            self.phase = 'starting'
            self.target_name = name
            self.serial = ''
            self.message = '에뮬레이터를 시작하고 있습니다.'
            self.worker = threading.Thread(target=self._boot, args=(name,), daemon=True)
            self.worker.start()
            return self.status()

    def _boot(self, name):
        try:
            device.boot(name)
            deadline = time.monotonic() + self.boot_timeout
            while time.monotonic() < deadline:
                running = device.find_avd(name)
                if running and running['state'] == 'device':
                    with self.lock:
                        self.serial = running['serial']
                        self.message = 'Android 부팅을 확인하고 있습니다.'
                    if device.boot_completed(running['serial']):
                        with self.lock:
                            self.phase = 'ready'
                            self.message = '에뮬레이터가 준비됐습니다.'
                        return
                time.sleep(2)
            raise ValueError('에뮬레이터 부팅 시간이 초과됐습니다. 다시 준비해 주세요.')
        except ValueError as exc:
            with self.lock:
                self.phase = 'error'
                self.serial = ''
                self.message = device.redact(str(exc))

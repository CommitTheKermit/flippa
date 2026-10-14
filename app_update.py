"""Configured Android app source update, build, and emulator installation."""
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import threading

import device


def _now():
    return datetime.now(timezone.utc).isoformat()


def _relative_setting(settings, name, default):
    value = settings.get(name, default)
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or not value.strip():
        raise ValueError(f'{name}은 저장소 안의 상대 경로여야 합니다.')
    return path


class AppUpdater:
    """Updates one trusted checkout and installs its debug APK on one emulator."""

    def __init__(self, environ=None):
        settings = os.environ if environ is None else environ
        raw_repo = settings.get('PLIPA_APP_REPO', '').strip()
        self.repo = Path(raw_repo).expanduser() if raw_repo else None
        if self.repo is not None and not self.repo.is_absolute():
            raise ValueError('PLIPA_APP_REPO는 절대 경로여야 합니다.')
        self.name = settings.get('PLIPA_APP_NAME', '책췍').strip() or '책췍'
        self.branch = settings.get('PLIPA_APP_BRANCH', 'an-develop').strip()
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,199}', self.branch) or '..' in self.branch:
            raise ValueError('PLIPA_APP_BRANCH 형식이 올바르지 않습니다.')
        self.project = _relative_setting(settings, 'PLIPA_APP_PROJECT', 'android')
        self.apk = _relative_setting(settings, 'PLIPA_APP_APK', 'android/app/build/outputs/apk/debug/app-debug.apk')
        self.task = settings.get('PLIPA_APP_GRADLE_TASK', ':app:assembleDebug').strip()
        if not re.fullmatch(r':(?:[A-Za-z0-9_-]+:)*[A-Za-z0-9_-]+', self.task):
            raise ValueError('PLIPA_APP_GRADLE_TASK 형식이 올바르지 않습니다.')
        self.package = settings.get('PLIPA_APP_PACKAGE', 'com.chamsae.chaekchaek').strip()
        if not re.fullmatch(r'[A-Za-z][\w]*(?:\.[\w]+)+', self.package):
            raise ValueError('PLIPA_APP_PACKAGE 형식이 올바르지 않습니다.')
        self.lock = threading.Lock()
        self.worker = None
        self.current = self._base_status()

    def _base_status(self):
        return {'available': self.repo is not None, 'running': False, 'phase': 'idle',
                'name': self.name, 'branch': self.branch, 'message':
                '업데이트할 앱이 설정되지 않았습니다.' if self.repo is None else '최신 APK를 빌드할 수 있습니다.'}

    def status(self):
        with self.lock:
            return dict(self.current)

    def start(self, serial):
        if self.repo is None:
            raise ValueError('서버에 앱 저장소를 먼저 설정하세요.')
        if not any(d['serial'] == serial and d['state'] == 'device' for d in device.devices()):
            raise ValueError('설치할 에뮬레이터를 선택하세요.')
        with self.lock:
            if self.current['running']:
                raise ValueError('앱 업데이트가 이미 진행 중입니다.')
            self.current = {**self._base_status(), 'running': True, 'phase': 'fetching',
                            'serial': serial, 'message': '원격 변경을 확인하고 있습니다.', 'started': _now()}
            self.worker = threading.Thread(target=self._run, args=(serial,), daemon=True)
            self.worker.start()
            return dict(self.current)

    def _set(self, phase, message, **extra):
        with self.lock:
            self.current.update({'phase': phase, 'message': device.redact(message), **extra})

    def _command(self, args, cwd, timeout=120):
        try:
            result = subprocess.run(args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    timeout=timeout, check=False)
        except FileNotFoundError:
            raise ValueError('Git 또는 Gradle 실행 도구를 찾지 못했습니다.') from None
        except subprocess.TimeoutExpired:
            raise ValueError('소스 갱신 또는 빌드 시간이 초과됐습니다.') from None
        output = device.redact(result.stdout.decode(errors='replace'))
        output = re.sub(r'(?i)(https?://)[^/@\s]+@', r'\1***@', output)
        output = re.sub(r'(?i)([?&](?:token|key|password|secret)=)[^&\s]+', r'\1***', output)
        if result.returncode:
            detail = output[-1200:].strip()
            raise ValueError(detail or '명령 실행에 실패했습니다.')
        return output

    def _git(self, *args, timeout=120):
        return self._command(['git', *args], self.repo, timeout).strip()

    def _run(self, serial):
        try:
            if not self.repo.is_dir() or not (self.repo/'.git').exists():
                raise ValueError('설정한 앱 저장소를 찾지 못했습니다.')
            if self._git('status', '--porcelain', '--untracked-files=no'):
                raise ValueError('앱 저장소에 커밋하지 않은 변경이 있어 중단했습니다.')
            if self._git('branch', '--show-current') != self.branch:
                raise ValueError(f'앱 저장소 브랜치를 {self.branch}(으)로 맞춰 주세요.')
            before = self._git('rev-parse', '--short=12', 'HEAD')
            self._git('fetch', '--prune', 'origin', self.branch, timeout=300)
            result = subprocess.run(['git', 'merge-base', '--is-ancestor', 'HEAD', f'origin/{self.branch}'],
                                    cwd=self.repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if result.returncode:
                raise ValueError('원격 브랜치로 fast-forward할 수 없어 소스를 변경하지 않았습니다.')
            self._git('merge', '--ff-only', f'origin/{self.branch}')
            revision = self._git('rev-parse', '--short=12', 'HEAD')
            self._set('building', f'{revision} 소스로 APK를 빌드하고 있습니다.', revision=revision,
                      source_changed=before != revision)
            project = self.repo/self.project
            wrapper = project/('gradlew.bat' if os.name == 'nt' else 'gradlew')
            if not wrapper.is_file():
                raise ValueError('Gradle Wrapper를 찾지 못했습니다.')
            self._command([str(wrapper), self.task, '--console=plain', '--no-daemon'], project, 1200)
            apk = self.repo/self.apk
            if not apk.is_file() or apk.stat().st_size == 0:
                raise ValueError('빌드가 끝났지만 설치할 APK를 찾지 못했습니다.')
            self._set('installing', f'{serial}에 APK를 설치하고 있습니다.', apk_bytes=apk.stat().st_size)
            device.adb(serial, 'install', '-r', str(apk), timeout=300)
            device.launch(serial, self.package)
            installed = device.environment(serial, self.package)['app']
            self._set('done', '최신 APK를 설치하고 앱을 열었습니다.', running=False,
                      installed=installed, finished=_now())
        except Exception as exc:
            self._set('error', str(exc), running=False, finished=_now())

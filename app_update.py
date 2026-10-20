"""Configured Android app source update, build, and emulator installation."""
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time

import device


def _now():
    return datetime.now(timezone.utc).isoformat()


def _relative_setting(settings, name, default):
    value = settings.get(name, default)
    path = Path(value)
    if path.is_absolute() or '..' in path.parts or not value.strip():
        raise ValueError(f'{name}은 저장소 안의 상대 경로여야 합니다.')
    return path


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


class ApkInspector:
    """Reads verified APK package, version, and public signing certificates."""

    def _tool(self, name):
        suffix = '.bat' if name == 'apksigner' else '.exe'
        filename = name + suffix if os.name == 'nt' else name
        candidates = list((device.SDK/'build-tools').glob('*/' + filename))
        if not candidates:
            raise ValueError('Android SDK Build Tools의 aapt와 apksigner가 필요합니다.')
        return str(sorted(candidates, key=lambda p: tuple(
            int(n) for n in re.findall(r'\d+', p.parent.name)))[-1])

    def inspect(self, apk):
        badging = device.run([self._tool('aapt'), 'dump', 'badging', str(apk)])
        package = re.search(r"^package: name='([^']+)' versionCode='(\d+)' versionName='([^']*)'", badging, re.M)
        if not package:
            raise ValueError('APK의 패키지와 버전을 확인하지 못했습니다.')
        certificate = device.run([self._tool('apksigner'), 'verify', '--print-certs', str(apk)])
        signers = sorted({s.lower() for s in re.findall(
            r'(?:Signer #\d+|V\d+ Signer):? certificate SHA-256 digest: ([0-9a-fA-F]{64})', certificate)})
        if not signers:
            raise ValueError('APK 서명을 확인하지 못했습니다.')
        return {'package': package[1], 'version_code': int(package[2]),
                'version_name': package[3], 'signer_sha256': ', '.join(signers)}


class ApkSigningPolicy:
    """Allows only server-configured public certificate fingerprints."""

    def __init__(self, fingerprints):
        self.allowed = self._fingerprints(fingerprints) if fingerprints.strip() else frozenset()

    @staticmethod
    def _fingerprints(value):
        fingerprints = []
        for item in value.split(','):
            fingerprint = item.strip()
            if not (re.fullmatch(r'[0-9a-fA-F]{64}', fingerprint) or
                    re.fullmatch(r'(?:[0-9a-fA-F]{2}:){31}[0-9a-fA-F]{2}', fingerprint)):
                raise ValueError('서명 SHA-256 지문은 64자리 16진수 또는 콜론 구분 형식이어야 합니다.')
            fingerprints.append(fingerprint.replace(':', '').lower())
        return frozenset(fingerprints)

    def require_configured(self):
        if not self.allowed:
            raise ValueError('서버에 PLIPA_APP_ALLOWED_SIGNER_SHA256 허용 서명 지문을 먼저 설정하세요.')

    def require_allowed(self, metadata):
        self.require_configured()
        signers = self._fingerprints(metadata.get('signer_sha256', ''))
        if not signers.issubset(self.allowed):
            raise ValueError('빌드한 APK에 서버에서 허용하지 않은 서명이 있습니다. 설치하거나 앱을 삭제하지 않았습니다.')


class AppUpdater:
    """Updates one trusted checkout and installs an allowed APK on one emulator."""

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
        self.signing_policy = ApkSigningPolicy(settings.get('PLIPA_APP_ALLOWED_SIGNER_SHA256', ''))
        self.lock = threading.Lock()
        self.worker = None
        self.inspector = ApkInspector()
        self.candidate = None
        self.artifact = None
        self.current = self._base_status()

    def _base_status(self):
        if self.repo is None:
            message = '업데이트할 앱이 설정되지 않았습니다.'
        elif not self.signing_policy.allowed:
            message = '서버에 PLIPA_APP_ALLOWED_SIGNER_SHA256 허용 서명 지문을 먼저 설정하세요.'
        else:
            message = '최신 APK를 빌드할 수 있습니다.'
        return {'available': self.repo is not None, 'running': False, 'phase': 'idle',
                'name': self.name, 'branch': self.branch, 'message': message}

    def status(self):
        with self.lock:
            return dict(self.current)

    def start(self, serial):
        if self.repo is None:
            raise ValueError('서버에 앱 저장소를 먼저 설정하세요.')
        self.signing_policy.require_configured()
        if not any(d['serial'] == serial and d['state'] == 'device' for d in device.devices()):
            raise ValueError('설치할 에뮬레이터를 선택하세요.')
        with self.lock:
            if self.current['running'] or (self.worker and self.worker.is_alive()):
                raise ValueError('앱 업데이트가 이미 진행 중입니다.')
            self._discard_candidate()
            self.current = {**self._base_status(), 'running': True, 'phase': 'fetching',
                            'serial': serial, 'message': '원격 변경을 확인하고 있습니다.', 'started': _now()}
            self.worker = threading.Thread(target=self._run, args=(serial,), daemon=True)
            self.worker.start()
            return dict(self.current)

    def _discard_candidate(self):
        self.candidate = None
        if self.artifact:
            self.artifact.cleanup()
            self.artifact = None

    def confirm_reinstall(self, serial, token, delete_data):
        with self.lock:
            candidate = self.candidate
            if (not candidate or self.current['running'] or (self.worker and self.worker.is_alive()) or
                    serial != candidate['serial'] or
                    not isinstance(token, str) or not token.isascii() or
                    not secrets.compare_digest(token, candidate['token'])):
                raise ValueError('재설치 대상이 바뀌었습니다. 최신 APK를 다시 빌드하세요.')
            if time.monotonic() >= candidate['expires']:
                self._discard_candidate()
                self.current.update(phase='error', reinstall_token=None,
                                    message='재설치 확인 시간이 만료됐습니다. 최신 APK를 다시 빌드하세요.')
                raise ValueError(self.current['message'])
            if delete_data is not True and delete_data is not False:
                raise ValueError('앱 데이터 삭제 여부를 명시적으로 확인하세요.')
            self.candidate = None  # Confirmation is single use, including cancellation and failures.
            self.current.update(reinstall_token=None)
            if delete_data is False:
                self._discard_candidate()
                self.current.update(phase='cancelled', message='재설치를 취소했습니다. 기존 앱과 데이터를 유지합니다.')
                return dict(self.current)
            self.current.update(running=True, phase='reinstalling',
                                message='확인한 APK와 기존 앱이 그대로인지 확인하고 있습니다.')
            self.worker = threading.Thread(target=self._reinstall, args=(candidate,), daemon=True)
            self.worker.start()
            return dict(self.current)

    def _installed_apk(self, serial, directory):
        paths = device.adb(serial, 'shell', 'pm', 'path', self.package).splitlines()
        paths = [line.removeprefix('package:').strip() for line in paths if line.startswith('package:')]
        if not paths:
            raise ValueError('기존 앱을 확인하지 못했습니다. 앱을 삭제하지 않았습니다.')
        # The base APK identifies this install even for split APKs.
        base = next((path for path in paths if path.endswith('/base.apk')), paths[0])
        target = directory/'installed.apk'
        device.adb(serial, 'pull', base, str(target), timeout=120)
        return target

    def _device_identity(self, serial):
        identity = device.adb(serial, 'shell', 'settings', 'get', 'secure', 'android_id').strip()
        if not re.fullmatch(r'[0-9a-fA-F]{1,16}', identity):
            raise ValueError('설치 대상 기기를 식별하지 못했습니다. 앱을 삭제하지 않았습니다.')
        return identity

    def _offer_reinstall(self, serial, apk, metadata):
        installed = self._installed_apk(serial, Path(self.artifact.name))
        installed_metadata = self.inspector.inspect(installed)
        if installed_metadata['package'] != self.package:
            raise ValueError('기존 앱 패키지를 확인하지 못했습니다. 앱을 삭제하지 않았습니다.')
        token = secrets.token_urlsafe(24)
        candidate = {'serial': serial, 'token': token, 'expires': time.monotonic() + 600,
                     'apk': apk, 'apk_sha256': _sha256(apk), 'installed_sha256': _sha256(installed),
                     'device_identity': self._device_identity(serial)}
        with self.lock:
            self.candidate = candidate
            self.current.update(phase='signature_mismatch', running=False, reinstall_token=token,
                package=self.package, apk_version=metadata['version_name'],
                signer_sha256=metadata['signer_sha256'],
                installed_version=installed_metadata['version_name'],
                installed_signer_sha256=installed_metadata['signer_sha256'],
                message='기존 앱과 새 APK의 서명이 달라 업데이트할 수 없습니다. '
                        '데이터를 유지하려면 기존 앱과 같은 서명 키로 빌드하세요. '
                        '삭제 후 재설치를 선택하면 로그인 정보와 기기 내 앱 데이터가 삭제됩니다.')

    def _finish_install(self, serial, reinstalled=False):
        device.launch(serial, self.package)
        installed = device.environment(serial, self.package)['app']
        self._set('done', '기존 앱을 삭제하고 새 APK를 설치해 열었습니다.' if reinstalled else
                  '앱 데이터를 유지하며 최신 APK를 설치하고 앱을 열었습니다.',
                  running=False, installed=installed, finished=_now())

    def _reinstall(self, candidate):
        deleted = False
        deletion_started = False
        try:
            apk, serial = candidate['apk'], candidate['serial']
            if _sha256(apk) != candidate['apk_sha256']:
                raise ValueError('확인한 APK가 바뀌었습니다. 앱을 삭제하지 않았습니다.')
            # Verify the signed artifact again before any destructive action.
            metadata = self.inspector.inspect(apk)
            if metadata['package'] != self.package:
                raise ValueError('APK 패키지가 다릅니다. 앱을 삭제하지 않았습니다.')
            self.signing_policy.require_allowed(metadata)
            if self._device_identity(serial) != candidate['device_identity']:
                raise ValueError('설치 대상 기기가 바뀌었습니다. 앱을 삭제하지 않았습니다.')
            with tempfile.TemporaryDirectory(prefix='plipa-installed-') as directory:
                installed = self._installed_apk(serial, Path(directory))
                if _sha256(installed) != candidate['installed_sha256']:
                    raise ValueError('기존 앱이 바뀌었습니다. 삭제하지 않았습니다. 최신 APK를 다시 빌드하세요.')
            self._set('reinstalling', '확인한 기존 앱과 기기 내 데이터를 삭제하고 있습니다.')
            deletion_started = True
            result = device.adb(serial, 'uninstall', self.package, timeout=120)
            if result.strip() != 'Success':
                raise ValueError('기존 앱 삭제를 확인하지 못했습니다. 재설치를 중단했습니다.')
            deleted = True
            self._set('reinstalling', '기존 앱 삭제 완료. 확인한 APK를 설치하고 있습니다.', data_deleted=True)
            device.adb(serial, 'install', str(apk), timeout=300)
            self._finish_install(serial, reinstalled=True)
        except Exception as exc:
            prefix = ('기존 앱과 데이터는 삭제됐지만 재설치를 완료하지 못했습니다. ' if deleted else
                      '앱 삭제 요청 후 결과를 확인하지 못했습니다. 데이터 삭제 여부를 확인하세요. ' if deletion_started else '')
            self._set('error', prefix + str(exc), running=False,
                      data_deleted=deleted if deleted or not deletion_started else None, finished=_now())
        finally:
            with self.lock:
                self._discard_candidate()

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
            self.artifact = tempfile.TemporaryDirectory(prefix='plipa-app-')
            snapshot = Path(self.artifact.name)/'update.apk'
            shutil.copyfile(apk, snapshot)
            metadata = self.inspector.inspect(snapshot)
            if metadata['package'] != self.package:
                raise ValueError('빌드한 APK의 패키지가 설정과 다릅니다. 설치하지 않았습니다.')
            self.signing_policy.require_allowed(metadata)
            self._set('installing', f'{serial}에 데이터를 유지하며 APK를 설치하고 있습니다.',
                      apk_bytes=snapshot.stat().st_size, apk_version=metadata['version_name'],
                      signer_sha256=metadata['signer_sha256'])
            try:
                device.adb(serial, 'install', '-r', str(snapshot), timeout=300)
            except ValueError as exc:
                if 'INSTALL_FAILED_UPDATE_INCOMPATIBLE' not in str(exc):
                    raise
                self._offer_reinstall(serial, snapshot, metadata)
                return
            self._finish_install(serial)
        except Exception as exc:
            self._set('error', str(exc), running=False, finished=_now())
        finally:
            with self.lock:
                if self.candidate is None:
                    self._discard_candidate()

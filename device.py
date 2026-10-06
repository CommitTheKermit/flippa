"""Local emulator transport. No arbitrary shell commands from the browser or AI."""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET

import emulator

DEFAULT_SDK = ((Path(os.environ['LOCALAPPDATA']) if os.environ.get('LOCALAPPDATA') else Path.home()) / 'Android/Sdk'
               if os.name == 'nt' else Path.home() / 'Library/Android/sdk')
SDK = Path(os.environ.get('ANDROID_HOME') or os.environ.get('ANDROID_SDK_ROOT') or DEFAULT_SDK)
ADB = shutil.which('adb') or str(SDK / 'platform-tools' / ('adb.exe' if os.name == 'nt' else 'adb'))
EMULATOR = str(SDK / 'emulator' / ('emulator.exe' if os.name == 'nt' else 'emulator'))
PACKAGE = 'com.chamsae.chaekchaek'


def redact(text):
    text = re.sub(r'(?i)(bearer\s+)\S+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)([\"\']?(?:[\w.-]*(?:token|password|secret|api[_-]?key)|authorization|cookie|비밀번호)[\"\']?\s*[:=]\s*)(\"[^\"]*\"|\'[^\']*\'|[^\s,;&<]+)', r'\1[REDACTED]', text)
    text = re.sub(r'\beyJ[\w-]+\.[\w-]+\.[\w-]+\b|\bsk-[\w-]{12,}\b', '[REDACTED]', text)
    return text


def run(args, timeout=20, binary=False):
    try:
        p = subprocess.run(args, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise ValueError('필요한 실행 도구가 없습니다. 설치 안내를 확인하세요.') from None
    except subprocess.TimeoutExpired:
        raise ValueError('기기 응답 시간이 초과됐습니다. 연결 상태를 확인하세요.') from None
    if p.returncode:
        raise ValueError(redact(p.stderr.decode(errors='replace')[-800:]) or '명령 실행 실패')
    return p.stdout if binary else p.stdout.decode(errors='replace')


def devices():
    result = []
    for line in run([ADB, 'devices', '-l']).splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and re.fullmatch(r'emulator-\d+', fields[0]):
            result.append({'serial': fields[0], 'state': fields[1], 'label': fields[0]})
    return result


def adb(serial, *args, **kwargs):
    if not re.fullmatch(r'emulator-\d+', serial):
        raise ValueError('로컬 에뮬레이터만 연결할 수 있습니다.')
    return run([ADB, '-s', serial, *args], **kwargs)


def avds():
    return run([EMULATOR, '-list-avds']).strip().splitlines() if Path(EMULATOR).exists() else []


def _number_setting(name, default, low, high):
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        value = default
    return str(min(high, max(low, value)))


def avd_name(serial):
    try:
        lines = adb(serial, 'emu', 'avd', 'name', timeout=5).strip().splitlines()
    except ValueError:
        lines = []  # A hung emulator's console fails, but its discovery file still names it.
    return lines[0] if lines else emulator.discovery(serial)[1].get('avd.name', '')


def find_avd(name):
    for entry in devices():
        try:
            if avd_name(entry['serial']) == name:
                return entry
        except ValueError:
            continue
    return None


def boot_completed(serial):
    try:
        return adb(serial, 'shell', 'getprop', 'sys.boot_completed', timeout=5).strip() == '1'
    except ValueError:
        return False


def stop(serial):
    try:
        adb(serial, 'emu', 'kill', timeout=5)
        return
    except ValueError:
        pass
    # A hung emulator ignores its console and keeps the AVD lock; end the process directly.
    path, _ = emulator.discovery(serial)
    if not path:
        raise ValueError('에뮬레이터 프로세스를 찾지 못했습니다.')
    try:
        os.kill(int(path.stem.split('_')[1]), getattr(signal, 'SIGKILL', signal.SIGTERM))
    except (OSError, ValueError):
        pass  # Already gone.
    path.unlink(missing_ok=True)


def _optimize_when_ready(name):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        try:
            for entry in devices():
                if entry['state'] == 'device' and avd_name(entry['serial']) == name:
                    for setting in ('window_animation_scale', 'transition_animation_scale', 'animator_duration_scale'):
                        adb(entry['serial'], 'shell', 'settings', 'put', 'global', setting, '0')
                    return
        except ValueError:
            pass
        time.sleep(2)


def boot(name):
    if name not in avds():
        raise ValueError('등록된 가상 기기를 선택하세요.')
    args = [EMULATOR, '-avd', name, '-no-window', '-no-audio', '-no-boot-anim',
            '-gpu', os.environ.get('PLIPA_EMULATOR_GPU', 'auto'),
            '-cores', _number_setting('PLIPA_EMULATOR_CORES', 2, 1, 4),
            '-memory', _number_setting('PLIPA_EMULATOR_MEMORY_MB', 3072, 1024, 4096),
            '-no-snapshot',  # Quickboot never restored on the home server; skip snapshot load and save.
            '-grpc-use-token']
    # Keep this and the previous boot's output: a crash is usually followed by a re-prepare.
    log_path = Path(tempfile.gettempdir())/'plipa-emulator.log'
    try:
        os.replace(log_path, log_path.with_suffix('.prev.log'))
    except OSError:
        pass  # No previous log, or a dying emulator still holds it on Windows.
    with open(log_path, 'wb') as log:
        subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    threading.Thread(target=_optimize_when_ready, args=(name,), daemon=True).start()
    return name


def screenshot(serial):
    data = adb(serial, 'exec-out', 'screencap', '-p', binary=True)
    if not data.startswith(b'\x89PNG\r\n\x1a\n'):
        raise ValueError('화면을 가져오지 못했습니다.')
    return data


def tree(serial):
    adb(serial, 'shell', 'uiautomator', 'dump', '/sdcard/plipa-window.xml', timeout=30)
    xml = adb(serial, 'exec-out', 'cat', '/sdcard/plipa-window.xml')
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise ValueError('화면 구조를 읽지 못했습니다. 화면 전환 후 다시 시도하세요.') from None
    nodes = []
    for elem in root.iter('node'):
        a = elem.attrib
        if a.get('password') == 'true':
            continue
        bounds = [int(v) for v in re.findall(r'\d+', a.get('bounds', ''))]
        if len(bounds) != 4 or bounds[0] >= bounds[2] or bounds[1] >= bounds[3]:
            continue
        text = redact(a.get('text', '') or a.get('content-desc', ''))
        nodes.append({'id': len(nodes), 'text': text, 'bounds': bounds,
                      'resource': a.get('resource-id', ''), 'package': a.get('package', ''),
                      'clickable': a.get('clickable') == 'true', 'enabled': a.get('enabled') == 'true'})
    return nodes


def node_for(action, nodes):
    target = action.get('target')
    if type(target) is not int or not 0 <= target < len(nodes):
        raise ValueError('현재 화면에 없는 조작 대상입니다.')
    node = nodes[target]
    if not node['enabled']:
        raise ValueError('비활성화된 대상입니다.')
    return node


def auto_allowed(action):
    # UI labels are untrusted: a button named "검색" can still mutate data.
    return action['kind'] in ('observe', 'done')


def perform(serial, action, nodes=None):
    kind = action.get('kind')
    if kind == 'tap' and nodes is not None:
        x1, y1, x2, y2 = node_for(action, nodes)['bounds']
        args = ['tap', str((x1 + x2)//2), str((y1 + y2)//2)]
    elif kind in ('tap', 'swipe'):
        coords = action.get('coords')
        count = 2 if kind == 'tap' else 4
        if not isinstance(coords, list) or len(coords) != count or any(type(v) is not int or not 0 <= v <= 10000 for v in coords):
            raise ValueError('잘못된 화면 좌표입니다.')
        args = [kind, *map(str, coords)] + (['350'] if kind == 'swipe' else [])
    elif kind == 'text':
        value = action.get('text', '')
        if not isinstance(value, str) or not value or len(value) > 300 or not value.isascii() or any(ord(c) < 32 for c in value):
            raise ValueError('화면 입력은 영문·숫자 300자까지 지원합니다. 한글은 에뮬레이터 키보드를 사용하세요.')
        if redact(value) != value:
            raise ValueError('비밀값은 입력하거나 기록할 수 없습니다.')
        args = ['text', shlex.quote(value.replace(' ', '%s'))]
    elif kind == 'key':
        key = action.get('text')
        if key not in ('BACK', 'HOME', 'ENTER', 'DEL'):
            raise ValueError('지원하지 않는 키입니다.')
        args = ['keyevent', 'KEYCODE_' + key]
    elif kind in ('observe', 'done'):
        return
    else:
        raise ValueError('지원하지 않는 조작입니다.')
    adb(serial, 'shell', 'input', *args)


def logs(serial, package):
    pid = adb(serial, 'shell', 'pidof', package).strip().split()
    if not pid:
        raise ValueError('앱 프로세스가 없어 로그를 수집하지 못했습니다.')
    return redact(adb(serial, 'logcat', '-d', '-t', '200', '--pid=' + pid[0], '-v', 'threadtime'))


def launch(serial, package):
    if not re.fullmatch(r'[a-zA-Z][\w]*(?:\.[\w]+)+', package):
        raise ValueError('올바른 앱 패키지 이름을 입력하세요.')
    adb(serial, 'shell', 'monkey', '-p', package, '-c', 'android.intent.category.LAUNCHER', '1')


def environment(serial, package):
    return {'serial': serial, 'package': package,
            'android': adb(serial, 'shell', 'getprop', 'ro.build.version.release').strip(),
            'model': adb(serial, 'shell', 'getprop', 'ro.product.model').strip(),
            'app': '\n'.join(line.strip() for line in adb(serial, 'shell', 'dumpsys', 'package', package).splitlines() if 'versionName=' in line or 'versionCode=' in line)}

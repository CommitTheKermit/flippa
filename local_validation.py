"""Local Android tree -> Jev -> guarded action -> independent postcondition CLI."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import device
from jev_client import JevClient, DecisionFailure

SELECTOR_FIELDS = {'resource', 'text', 'class', 'checked', 'selected', 'enabled'}


def validate_selector(selector):
    if not isinstance(selector, dict) or not selector or set(selector) - SELECTOR_FIELDS:
        raise ValueError('선택자는 resource/text/class/checked/selected/enabled만 지원합니다.')
    if not any(isinstance(selector.get(k), str) and selector[k] for k in ('resource', 'text')):
        raise ValueError('선택자에는 정확한 resource 또는 text가 필요합니다.')
    for key, value in selector.items():
        if key in ('checked', 'selected', 'enabled'):
            if type(value) is not bool:
                raise ValueError('상태 선택자는 Boolean이어야 합니다.')
        elif not isinstance(value, str) or len(value) > 500:
            raise ValueError('잘못된 선택자 문자열입니다.')


def validate_scenario(scenario):
    if not isinstance(scenario, dict) or set(scenario) != {'goal', 'package', 'allowed_actions', 'expect'}:
        raise ValueError('goal, package, allowed_actions, expect 필드를 지정하세요.')
    if not isinstance(scenario['goal'], str) or not 1 <= len(scenario['goal']) <= 2000:
        raise ValueError('목표는 1~2000자입니다.')
    if not isinstance(scenario['package'], str) or not re.fullmatch(r'[A-Za-z]\w*(?:\.\w+)+', scenario['package']):
        raise ValueError('올바른 패키지가 필요합니다.')
    actions = scenario['allowed_actions']
    if not isinstance(actions, list) or not 1 <= len(actions) <= 100:
        raise ValueError('허용 행동은 1~100개입니다.')
    for action in actions:
        if not isinstance(action, dict) or set(action) - {'kind', 'selector', 'text', 'description'}:
            raise ValueError('잘못된 행동 필드입니다.')
        if action.get('kind') not in ('tap', 'text', 'scroll_up', 'scroll_down', 'back', 'enter'):
            raise ValueError('지원하지 않는 행동입니다.')
        validate_selector(action.get('selector'))
        if not isinstance(action.get('description'), str) or not 1 <= len(action['description']) <= 500:
            raise ValueError('행동의 의도와 영향을 description에 명시하세요.')
        if action['kind'] == 'text':
            value = action.get('text')
            if not isinstance(value, str) or not 1 <= len(value) <= 300 or not re.fullmatch(r'[A-Za-z0-9 .,!?@_+:/=-]+', value):
                raise ValueError('현재 입력은 안전한 ASCII 문자 300자까지 지원합니다. 한글 입력은 미지원입니다.')
        elif 'text' in action:
            raise ValueError('text 입력값은 text 행동에서만 허용합니다.')
    if not isinstance(scenario['expect'], list) or not 1 <= len(scenario['expect']) <= 20:
        raise ValueError('성공 조건은 1~20개의 정확한 선택자입니다.')
    for selector in scenario['expect']:
        validate_selector(selector)
    encoded = json.dumps(scenario, ensure_ascii=False)
    if device.redact(encoded) != encoded:
        raise ValueError('시나리오에 비밀값을 넣을 수 없습니다.')
    return scenario


def matching(nodes, selector):
    return [node for node in nodes if all(node.get(k) == v for k, v in selector.items())]


def completed(nodes, scenario):
    return all(len(matching(nodes, selector)) == 1 for selector in scenario['expect'])


def fingerprint(nodes):
    return hashlib.sha256(json.dumps(nodes, sort_keys=True).encode()).hexdigest()


def candidates_for(nodes, scenario):
    candidates = {}
    for index, action in enumerate(scenario['allowed_actions']):
        matches = matching(nodes, action['selector'])
        if len(matches) != 1 or not matches[0].get('enabled'):
            continue
        node = matches[0]
        kind = action['kind']
        if kind == 'tap' and not node.get('clickable'):
            continue
        if kind in ('text', 'enter') and not (node.get('editable') and node.get('focused')):
            continue
        if kind.startswith('scroll') and not node.get('scrollable'):
            continue
        candidates[f'a{index}'] = {'description': action['description'], 'action': action, 'node': node}
    return candidates


class AndroidDevice:
    def __init__(self, serial, package):
        self.serial, self.package = serial, package

    def observe(self):
        window = device.adb(self.serial, 'shell', 'dumpsys', 'window', 'windows')
        focus = next((line for line in window.splitlines() if 'mCurrentFocus=' in line), '')
        if not re.search(r'\b' + re.escape(self.package) + r'/', focus):
            raise ValueError('대상 앱이 전면에 없습니다. 외부 앱이나 시스템 창에서는 중단합니다.')
        return [n for n in device.tree(self.serial) if n['package'] == self.package]

    def execute(self, candidate):
        action, node = candidate['action'], candidate['node']
        kind = action['kind']
        if kind == 'tap':
            device.perform(self.serial, {'kind': 'tap', 'target': 0}, [node])
        elif kind == 'text':
            device.perform(self.serial, {'kind': 'text', 'text': action['text']})
        elif kind in ('back', 'enter'):
            device.perform(self.serial, {'kind': 'key', 'text': kind.upper()})
        else:
            x1, y1, x2, y2 = node['bounds']
            x = (x1 + x2) // 2
            top, bottom = y1 + (y2-y1)//4, y2 - (y2-y1)//4
            ys = (bottom, top) if kind == 'scroll_down' else (top, bottom)
            device.perform(self.serial, {'kind': 'swipe', 'coords': [x, ys[0], x, ys[1]]})

    def screenshot(self):
        return device.screenshot(self.serial)


class ValidationRun:
    def __init__(self, scenario, transport, chooser, max_steps=20, threshold=.85, sleep=time.sleep):
        self.scenario, self.device, self.chooser = scenario, transport, chooser
        self.max_steps, self.threshold, self.sleep = max_steps, threshold, sleep
        self.trace, self.last_nodes = [], []

    def run(self):
        seen = Counter()
        executed = set()
        started = time.monotonic()
        def finish(status, reason):
            return {'status': status, 'reason': reason, 'elapsed_seconds': round(time.monotonic()-started, 3),
                    'steps': len(self.trace), 'trace': self.trace, 'last_tree': self.last_nodes}
        try:
            nodes = self.device.observe()
            for _ in range(self.max_steps):
                self.last_nodes = nodes
                if len(nodes) > 500:
                    return finish('blocked', '화면 노드가 500개를 초과해 범위를 좁혀야 합니다.')
                if completed(nodes, self.scenario):
                    return finish('passed', '명시된 모든 트리 성공 조건을 확인했습니다.')
                before = fingerprint(nodes)
                seen[before] += 1
                if seen[before] >= 3:
                    return finish('blocked', '같은 상태가 반복되어 중단했습니다.')
                candidates = candidates_for(nodes, self.scenario)
                state = {'goal': self.scenario['goal'], 'screen': nodes,
                         'history': self.trace[-10:], 'expect': self.scenario['expect']}
                choice, confidence, probability = self.chooser.choose(state, candidates)
                if confidence < self.threshold or probability < self.threshold:
                    return finish('blocked', '선택 확신도 또는 확률이 기준보다 낮습니다.')
                if choice in ('done', 'blocked'):
                    return finish('blocked', '모델 종료 판단과 별개로 성공 조건이 충족되지 않았습니다.')
                if choice == 'wait':
                    self.sleep(.5)
                    nodes = self.device.observe()
                    self.trace.append({'action': 'wait', 'changed': fingerprint(nodes) != before})
                    continue
                if choice not in candidates:
                    return finish('blocked', '현재 후보에 없는 행동입니다.')
                # API round-trip may have outlived this observation. Never use stale coordinates.
                fresh = self.device.observe()
                if fingerprint(fresh) != before:
                    self.trace.append({'action': 'reobserve', 'changed': True})
                    nodes = fresh
                    continue
                if (before, choice) in executed:
                    return finish('blocked', '동일 상태에서 같은 조작을 다시 실행하지 않습니다.')
                executed.add((before, choice))
                self.device.execute(candidates[choice])
                nodes = fresh
                for attempt in range(4):
                    self.sleep(.25)
                    nodes = self.device.observe()
                    if fingerprint(nodes) != before:
                        break
                self.trace.append({'action': choice, 'kind': candidates[choice]['action']['kind'],
                                   'confidence': confidence, 'probability': probability,
                                   'changed': fingerprint(nodes) != before})
            self.last_nodes = nodes
            if completed(nodes, self.scenario):
                return finish('passed', '마지막 행동 후 모든 트리 성공 조건을 확인했습니다.')
            return finish('blocked', '최대 단계 수에 도달했습니다.')
        except (ValueError, OSError):
            # Transport/provider errors can contain UI text or credentials. Do not log raw errors.
            return finish('error', '관찰·판단·실행 중 오류가 발생했습니다. 자동 재실행하지 않습니다.')
        except KeyboardInterrupt:
            return finish('cancelled', '사용자가 실행을 중지했습니다.')


def require_local_ownership(serial):
    queue = Path.home()/'.agents/skills/mobile-validation-queue/scripts/validation_queue.py'
    if not queue.exists() or subprocess.run([sys.executable, str(queue), 'check'], capture_output=True).returncode:
        raise ValueError('mobile-validation-queue 실행기 안에서 실행하세요.')
    owner = os.environ.get('CODEX_THREAD_ID') or os.environ.get('CODEX_SESSION_ID')
    lease_root = Path(os.environ.get('CODEX_ANDROID_LEASE_DIR', str(Path.home()/'.codex/android-emulator-leases')))
    leases = [json.loads(p.read_text()) for p in lease_root.glob('*.json')]
    if not owner or not any(l.get('owner') == owner and l.get('serial') == serial for l in leases):
        raise ValueError('현재 세션에 바인딩된 Android 기기 임대가 필요합니다.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scenario', type=Path, required=True)
    parser.add_argument('--serial', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--execute', action='store_true', help='검토한 allowed_actions 실행과 트리 API 전송 허용')
    parser.add_argument('--max-steps', type=int, default=20)
    parser.add_argument('--threshold', type=float, default=.85)
    parser.add_argument('--capture-on-failure', action='store_true')
    args = parser.parse_args(argv)
    scenario = validate_scenario(json.loads(args.scenario.read_text()))
    if not 1 <= args.max_steps <= 100 or not .5 <= args.threshold <= 1:
        raise ValueError('max-steps 1~100, threshold 0.5~1 범위를 사용하세요.')
    if not re.fullmatch(r'emulator-\d+', args.serial):
        raise ValueError('첫 버전은 임대한 로컬 Android 에뮬레이터를 지원합니다.')
    if not args.execute:
        print(json.dumps({'status': 'validated', 'actions': len(scenario['allowed_actions']),
                          'message': '시나리오 형식만 확인했습니다. API·ADB 호출 없음.'}, ensure_ascii=False))
        return 0
    chooser = JevClient(os.environ.get('TYPESAFE_API_KEY'))
    require_local_ownership(args.serial)
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    transport = AndroidDevice(args.serial, scenario['package'])
    result = ValidationRun(scenario, transport, chooser, args.max_steps, args.threshold).run()
    result.update(api_calls=chooser.calls, usage=chooser.usage, serial=args.serial, package=scenario['package'])
    if args.capture_on_failure and result['status'] in ('blocked', 'error'):
        try:
            (args.output/'failure.png').write_bytes(transport.screenshot())
            result['screenshot'] = 'failure.png'
        except (ValueError, OSError):
            result['screenshot'] = 'unavailable'
    (args.output/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: result[k] for k in ('status', 'reason', 'steps', 'api_calls', 'usage')}, ensure_ascii=False))
    return 0 if result['status'] == 'passed' else 2


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError):
        print('설정 오류: 시나리오, API 키 환경변수, 출력 경로와 대기열·기기 임대를 확인하세요.', file=sys.stderr)
        sys.exit(2)

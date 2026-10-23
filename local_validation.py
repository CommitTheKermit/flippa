"""Compatibility entry point for the shared, pinned Jev runtime."""
from pathlib import Path
import subprocess
import sys


def main(argv=None):
    launcher = Path.home()/'.agents/skills/jev-local-validation/scripts/run.py'
    if not launcher.is_file():
        print('Jev 공용 실행기가 없습니다. jev-local-validation 스킬을 동기화하세요.', file=sys.stderr)
        return 2
    args = list(sys.argv[1:] if argv is None else argv)
    # The shared launcher enforces Droidrun and agent coordination in an isolated runtime.
    return subprocess.run([sys.executable, str(launcher), *args], check=False).returncode


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError):
        print('설정 오류: 공용 실행기, 시나리오, 출력 경로와 대기열·기기 임대를 확인하세요.', file=sys.stderr)
        sys.exit(2)

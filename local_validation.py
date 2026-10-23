"""Compatibility entry point for the independent jev-mobile-use package."""
import sys


def main(argv=None):
    try:
        from jev_mobile_use.cli import main as run
    except ImportError:
        print('Jev 실행기가 없습니다. requirements-jev.txt로 설치하세요.', file=sys.stderr)
        return 2
    # Plipa keeps its existing queue/lease contract, even if the caller supplies another mode.
    args = list(sys.argv[1:] if argv is None else argv)
    return run([*args, '--coordination', 'agent'])


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (ValueError, OSError):
        print('설정 오류: 시나리오, API 키 환경변수, 출력 경로와 대기열·기기 임대를 확인하세요.', file=sys.stderr)
        sys.exit(2)

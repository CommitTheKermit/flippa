# 원격 Android 검증

## 목적과 범위

Mac에서 커밋 SHA와 고정 프로필을 접수하면 Windows 홈서버의 독립 예약 작업이 빌드, 단위 테스트, APK 서명 검사, 데이터 유지 설치, 앱 foreground 실행 확인을 순서대로 수행한다. SSH 연결 종료와 무관하게 작업을 유지하고 상태와 결과 ZIP을 돌려준다. 로그인, 화면별 사용자 시나리오, iOS 검증은 이 프로필의 완료 범위가 아니다.

## 구성

- `validation_jobs.py`: SQLite 접수, FIFO, 중복 방지와 프로세스 간 잠금.
- `validation_cli.py`: 인증된 SSH 채널의 JSON 접수, 상태, 대기, 취소, 결과 반환.
- `validation_worker.py`: 허용 원격 브랜치의 정확한 SHA를 detached worktree로 빌드하고 검증.
- `validation_device_lease.py`: 기존 AVD 임대 저장소와 호환되는 소유권 등록. 기존 임대는 만료 추정으로 탈취하지 않는다.
- `validation_resource.py`: 웹 작업과 worker의 공용 잠금 연결.
- `plipa-remote-validation` 스킬: Mac에서 SSH 클라이언트 사용. Mac 모바일 대기열 슬롯은 잡지 않는다.

클라이언트는 임의 명령, 저장소 경로, 빌드 task를 제출할 수 없다. 관리자가 Git 밖 설정의 `profiles`, `allowed_refs`, `package`, `signers`, `avd`를 고정한다. SHA는 40자리이며, 같은 `request_id`의 동일 요청은 기존 작업을 반환하고 내용을 바꾼 재접수는 거부한다.

## 비공개 설정

서버 JSON의 필수 항목은 `state_dir`, `app_repo`, `origin`, `profiles`, `signers`, `package`, `avd`다. 프로필은 `tasks`, 저장소 상대 `apk`, 선택 `timeout`을 가진다. `private_files`는 원본 checkout의 Git 제외 설정만 작업 checkout에 공급하며 종료 시 지운다. keystore 파일은 Git 밖 기존 절대 경로를 참조한다.

주소와 계정, 경로, 키는 저장소에 넣지 않는다. 서버와 worker 예약 작업은 같은 서비스 계정으로 실행하고 `PLIPA_VALIDATION_CONFIG`로 같은 설정을 읽는다. 클라이언트 설정은 `~/.config/plipa-remote/client.json`에 저장하며 `host`, `python`, `cli`, `server_config`만 둔다. 파일 접근 권한을 계정으로 제한한다.

## 접수와 결과

스킬의 `remote_validation.py submit --sha <SHA> --profile android-smoke --request-id <고유값>`으로 접수하고 `wait <job>`, `status <job>`으로 상태를 확인한다. `cancel <job>`은 대기 작업을 취소하거나 실행 작업에 취소 요청을 남긴다. `results <job> --output <새 ZIP 경로>`는 테스트 XML, 정제한 실행 로그, smoke 결과, 소스와 APK 해시를 반환한다. APK 자체와 키 설정은 반환하지 않는다.

`queued`, `running`, `cancelling`은 완료가 아니다. `succeeded`는 실제 테스트가 1개 이상 통과하고 허용 APK 서명, 설치, 프로세스와 foreground를 확인한 경우다. `failed`와 `cancelled`는 성공으로 취급하지 않는다. `blocked`는 잔존 프로세스나 기기의 정리가 불명확해 다음 작업까지 차단한 상태다.

## 중단과 복구

worker는 하나만 실행한다. 전체 빌드와 기기 정리 동안 공용 잠금을 유지한다. 기존 웹 QA, 앱 업데이트, 기기 준비가 진행 중이면 접수를 유지한 채 기다리고, worker가 실행 중이면 웹 변경 요청은 HTTP 409로 거부한다. 시작한 AVD만 명시적 serial로 종료하고 종료 확인 후 임대를 반환한다. 서명 불일치 또는 downgrade는 설치 실패로 남기며 자동 삭제하지 않는다.

worker 충돌 후 `running`, `cancelling`, `blocked`를 시간만으로 재실행하지 않는다. 운영자가 worker 예약 작업을 중지하고 해당 job의 프로세스와 `device-owner.json`, AVD 임대를 대조해 정리를 확인한 뒤 CLI `recover` 요청에 `confirmed_stopped: true`를 준다. 복구는 이전 작업을 실패로 확정할 뿐 재실행하지 않는다. 원하면 새 request_id로 다시 접수한다.

## 403과 예약 작업 진단

2026-10-22 점검에서 등록된 외부 origin의 `/api/state`는 HTTP 200, Host가 다른 loopback 요청은 HTTP 403이었다. trusted Host/Origin 검사를 유지하고 worker의 loopback 요청에 등록 Host와 Origin을 전달한다.

기존 `Plipa Server`는 실행 중이었으며 `IgnoreNew` 설정이었다. 마지막 결과 `0x800710E0`는 실행 요청 거부였으나 이벤트 로그를 조회할 수 없어 중복 실행 거부가 원인이라는 설명은 추정이다. 예약 상태, 실제 프로세스/리스너, API 응답을 별도로 확인한다.

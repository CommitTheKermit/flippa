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

## 배포 운영 확인

서버 Git은 배포 기능이 포함된 `28ab52f`에서 분리한 `codex/plipa-remote-validation`을 사용한다. main 직접 반영은 하지 않았다. 기존 `Plipa Server`의 실행 계정, 트리거, origin과 빌드 설정을 유지하고 `Plipa Validation Worker`를 별도 등록했다. 작업 설정 변경 전 XML은 Git 밖에 보관한다.

기존 서비스 action은 PowerShell `ExecutionPolicy Bypass`를 사용했다. action을 재작성할 때 이 프로세스 인자를 보존해야 하며 시스템 실행 정책을 변경하지 않는다. 예약 작업 중지 후 Python 자식이 남을 수 있으므로 예약 상태만 보고 재시작하지 않는다. 유휴 상태를 확인한 뒤 리스너의 실제 소유 프로세스 또는 `validation_worker.py`의 확인된 프로세스 트리만 정리하고 새 작업을 시작한다. 전체 Python/Java/adb 프로세스를 일괄 종료하지 않는다.

전용 `Plipa_Validation` AVD는 기존 AVD의 하드웨어·시스템 이미지 설정만 사용하며 사용자 데이터 디스크는 공유하지 않는다. 기존 AVD의 다른 작업 임대는 보존한다. 전용 AVD도 작업별 임대를 등록하고 종료 후 반환한다.

Windows에서 Git worktree 등록은 해제됐지만 읽기 전용 생성 파일이 남는 경우를 확인했다. worker는 자신이 만든 job 경로만 추가 정리하며 Kotlin 컴파일러를 Gradle 프로세스 안에서 실행한다. 정리에 실패하면 결과의 `cleanup_warning`을 확인하고 해당 경로를 점검한다.

## 실행 증거 (2026-10-22)

- 실행기 회귀 검사: Mac 87개 통과. Windows는 82개 통과, Unix 전용 5개 건너뜀.
- 앱 소스: `2a6f6bb47fa64c61e52cc90cb14c59297ad68582`. release APK `1.1.1`, versionCode `5`.
- 서버 프로필: `:app:assembleRelease`, `:app:testDebugUnitTest`, `:shared:testAndroidHostTest --rerun`. app 모듈 테스트는 NO-SOURCE이며 shared host 테스트 314개를 실제 재실행한다. XML 존재만으로 새 실행을 추정하지 않고 Gradle task의 FROM-CACHE 여부도 확인한다.
- 동일 request_id 재접수는 같은 job을 반환했다. 실행 중 뒤에 접수한 대기 job은 빌드 없이 취소됐다.
- worker 실행 중 등록 origin의 상태 조회는 HTTP 200, 정상 인증의 변경 요청은 409, 잘못된 CSRF는 403이었다.
- SSH 상태 조회 한 차례 실패 후 동일 job의 완료를 다시 확인했다. 원격 작업은 재접수하지 않았다.
- 실패 job과 최초 checkout 정리 경고는 원래 결과에 남기고, 수정 뒤의 새 job과 구분한다.

이 검증은 APK 빌드, 단위 테스트, 서명과 버전 비교, `install -r`, 앱 프로세스와 foreground, 기기 종료와 결과 회수까지다. 로그인 성공, 저장·탐색 사용자 흐름, 사용자 데이터 유지의 기능 검증과 iOS 검증은 포함하지 않는다. 설치 전 기기 상태는 `install-check.json`으로 구분한다. 기존 앱이 없는 결과를 데이터 유지 재설치의 증거로 사용하지 않는다.

최종 job `7420d9ac2a484c66bb56083b4ae3a7ac`는 실행 코드 `e3d764e`에서 `succeeded`로 완료됐다. shared test task에 FROM-CACHE 또는 UP-TO-DATE 표시 없이 314개가 통과했다. `install-check.json`에서 기존 앱과 새 APK의 패키지, versionCode 5, versionName 1.1.1, 허용 서명 일치를 확인하고 삭제 없이 업데이트했다. `MainActivity`와 프로세스 생존을 확인했으며 cleanup 경고는 없다.

완료 후 두 예약 작업은 Running, 웹 리스너는 활성 상태였다. 에뮬레이터 프로세스 0개, 최종 job checkout 제거, 전용 AVD 임대 반환과 기존 다른 AVD 임대 보존을 확인했다. 결과 ZIP 61개 항목을 Mac으로 회수했고 APK·키 설정과 원격 사용자 경로가 포함되지 않은 것을 검사했다. Mac은 원격 빌드 동안 로컬 모바일 실행 슬롯을 사용하지 않았다.

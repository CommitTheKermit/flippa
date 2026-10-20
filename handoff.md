# 플리파 APK 서명 고정 후속 작업

## 프로젝트 위치 / 브랜치
- 경로: `/Users/ujeonghyeon/Desktop/dev/myDev/plipa`
- 브랜치: `codex/chaekchaek-apk-update`, 최초 문서 작성 전 HEAD `2b43319`, 후속 구현 시작 HEAD `6e209f5`. 후속 구현 착수 시 추적 중인 변경 없음.
- 기존 APK 업데이트 작업의 후속 문서다. 구현 전 원격 병합 상태를 다시 확인한다.
- 커밋 규칙: 완료한 변경은 `type(scope): 한글 요약`으로 즉시 커밋하며 서명 트레일러를 넣지 않는다.

## 무엇을 만드는가
홈서버2 플리파의 책췍 APK 업데이트가 기존 Google 로그인 설정과 호환되는 서명을 유지하도록 고정한다. 최초 작업은 핸드오프 작성이었으며, 이후 구현과 서버 배포까지 진행했다. 기기 검증 상태는 아래 최신 기록을 기준으로 판단한다.

## 이번 작업에서 확정한 결정 (중요)
- 새 OAuth 환경은 등록하지 않는다. 이미 등록된 키를 사용한다. OAuth 설정은 변경하지 않았다.
- 실패한 서버 APK는 기존 OAuth 등록과 다른 서명이었다. 동일 APK를 기존 등록 키로 재서명해 설치했고, 사용자가 직접 로그인 성공을 확인했다. 서명 관련 영역을 제외한 ZIP 내용은 동일했다.
- 서명 충돌로 덮어쓰기 설치가 실패해 사용자 승인 후 Pixel_9a의 책췍 앱만 삭제하고 재설치했다. 실제 설치본도 다시 확인했다. 데이터 초기화도 함께 수행됐고 원래 로그인 예외의 타입/코드는 확보하지 못했다.
- 사용자가 직접 플리파를 조작하기로 해서 에이전트의 기기 조작은 중단했다. 후속 데이터 삭제에는 다시 승인이 필요하다.

## 재사용할 코어 / 건드리지 말 것
- `app_update.py`: 고정 서버 설정, fast-forward-only 갱신, APK 스냅샷, 패키지 검사와 삭제 확인 흐름을 보존한다.
- `ApkInspector.inspect()`는 유효한 서명을 읽지만 허용된 OAuth 서명인지 판정하지 않는다. 설치 성공과 로그인 성공을 구분한다.
- `test_app_update.py`의 서명 충돌, 일회용 확인, 대상 변경 시 삭제 차단 테스트를 확장한다.
- `AGENTS.md`와 `README.md`의 "책췍 APK 업데이트" 절을 먼저 읽는다.
- 등록된 공개 지문과 앱 서명 설정은 `/Users/ujeonghyeon/Desktop/dev/myDev/2026-chaekchaek/docs/android-google-login.md`와 같은 저장소의 `android/app/build.gradle.kts`를 참고한다. 앱의 릴리스 작업 브랜치와 OAuth/Play 설정은 임의 변경하지 않는다.

## 최초 핸드오프 시점의 상태
- 확인일: 2026-10-20. 제품 코드 변경과 새 의존성은 없다. 새 빌드나 플리파 테스트는 이번 문서 작업에서 실행하지 않았다.
- 앞선 원격 조사 시 플리파는 `c02eb40`, 앱 전용 checkout은 `an-develop`의 `e905fb101386`였다. 로컬 `2b43319`의 삭제 확인 기능은 당시 서버에 미배포였다. 문서 작성 중 원격 상태는 재확인하지 않았다.
- 서버 자동 빌드는 당시 기본 debug 작업/APK 경로였다. 당시에는 업데이트를 실행하면 실패했던 서명으로 돌아갈 수 있었다.
- 재서명 APK의 설치와 사용자 로그인 성공은 확인됐다. 새 release 빌드와 자동 업데이트 후 로그인은 미검증이다.
- 기기: `Pixel_9a`, serial `emulator-5554`, 앱 `com.chamsae.chaekchaek`, 버전 `1.1.1`/`5`. 기존 실행 상태와 당시 세션의 lease를 유지했으므로 다음 세션은 소유권을 확인한다.
- 서명용 비밀정보는 서버에 복사하지 않았다.

## 최초 구현 계획 (아래 최신 기록과 구분)
1. 저장소 상태와 브랜치 범위를 확인한다. 원격 작업 전 `home-server-2`, `android-emulator-lease` 스킬과 배포 절차를 읽는다. SSH 별칭은 `home-server-2`다.
2. 서버 예약 작업 `Plipa Server`와 앱 checkout의 release 서명 지원을 확인하고 기존 등록 키를 안전하게 공급할 방법을 정한다.
3. 제안 설정은 `PLIPA_APP_GRADLE_TASK=:app:assembleRelease`, `PLIPA_APP_APK=android/app/build/outputs/apk/release/app-release.apk`다. 실제 서명을 검증한 뒤 적용한다. 아직 미적용이며 모든 프로젝트의 기본값 변경을 뜻하지 않는다.
4. `app_update.py`에서 서버에 등록한 허용 인증서 지문과 빌드 APK를 설치 전에 비교하고 불일치 시 설치/삭제 전에 중단한다. 설정 이름과 다중 서명 처리 방식은 아직 미정이다.
5. 기존 삭제 확인과 스냅샷/기기 검사를 유지하고 허용 서명 통과, 다른 서명/미서명/잘못된 패키지 차단 및 삭제 경계 회귀 테스트를 추가한다. `.venv/bin/python -m unittest -q`와 `git diff --check`를 실행한다.
6. 서버 반영 후 서비스 응답, APK 패키지/버전/서명과 설치를 각각 검증한다. 자동 빌드 APK로 로그인과 반복 업데이트 시 데이터 유지를 확인한다. 사용자의 직접 조작을 우선하며 기기 조작이 필요하면 범위를 먼저 알린다.

## 최신 후속 구현 및 검증 (2026-10-20)

- `ApkSigningPolicy`로 허용 서명 판정을 분리했다. `PLIPA_APP_ALLOWED_SIGNER_SHA256`이 없으면 업데이트를 시작하지 않는다. SHA-256 지문은 64자리 16진수 또는 콜론 구분 형식을 사용하며 여러 지문은 쉼표로 구분한다. APK의 모든 서명자가 허용 목록에 있어야 한다.
- 빌드 APK 스냅샷 검사 후 설치 직전과, 확인받은 APK의 삭제·재설치 직전에 허용 지문을 검사한다. 다른 서명, 미서명 또는 잘못된 패키지는 설치·삭제 전에 중단한다. 서버 고정 설정, fast-forward-only 갱신, 스냅샷과 일회용 삭제 확인 경계를 유지했다.
- 원격 기본 브랜치와 비교한 결과 기존 APK 업데이트 커밋은 미병합 상태이며 관련 PR은 없었다. 같은 기능의 후속 작업으로 기존 `codex/chaekchaek-apk-update` 브랜치를 재사용했다. 새 이슈나 PR은 생성하지 않았다.
- 사용자 승인으로 기존 등록 키를 홈서버2에 공급했다. keystore는 Git 밖에 두었고 앱의 `keystore.properties`와 기존 release용 `google-services.json`은 Git 제외 상태를 확인했다. 키 디렉터리와 두 설정 파일의 ACL을 서비스 계정만 접근하도록 제한했다. 비밀값을 예약 작업 인자나 저장소에 넣지 않았다. OAuth/Play 설정과 앱 릴리스 작업 브랜치는 변경하지 않았다.
- 첫 release 빌드는 서버에 debug용 `google-services.json`만 있어 실패했다. 로컬 앱의 기존 release 설정을 공급한 후 재빌드가 성공했다. Google 로그인 코드는 공통 리소스의 웹 클라이언트 ID를 사용한다.
- 앱 전용 checkout은 `an-develop`을 fast-forward하여 `2a6f6bb47fa64c61e52cc90cb14c59297ad68582`로 갱신했다. 실제 release APK는 `com.chamsae.chaekchaek`, `1.1.1`/`5`, 12,452,480 bytes이며 서명 검증을 통과했다.
- 인증서 SHA-256은 `a0a10d8cc1245dc95f1468aa034d68595ca54df4972621fc97680234bc0ddb53`이다. 로컬 기존 키에서 읽은 SHA-1은 앱의 `docs/android-google-login.md`에 기록된 테스트 APK 등록 키와 일치했다. 이 지문만 서버 허용값으로 고정했다. APK 자체의 SHA-256은 `52dfba0089222957e4559159a594a7c52e484d5b0cab731cffc3c9a29a6c8581`이며 인증서 지문과 구분한다.
- 서버 예약 작업 `Plipa Server`에 `:app:assembleRelease`, `android/app/build/outputs/apk/release/app-release.apk`와 허용 지문을 반영했다. 기존 작업 정의는 Git 밖의 제한된 경로에 백업했다. 서비스 재시작 후 예약 작업 Running, 단일 리스너, HTTPS 페이지 및 `/api/state` 응답 200과 앱 업데이트 대기 상태를 확인했다. 다른 환경의 기본 Gradle 작업/APK 경로는 바꾸지 않았다.
- 로컬 `.venv/bin/python -m unittest -q` 70개와 `git diff --check`가 통과했다. 서버의 unittest도 통과했다. 실제 release APK를 새 `ApkInspector`와 `ApkSigningPolicy`로 검사하여 허용 지문 통과 및 다른 허용 지문 설정 시 차단을 확인했다. 새 의존성은 없다.
- `Pixel_9a`에는 이전 세션의 임대가 남아 있어 후속 구현 세션에서는 기기를 조작하지 않았다. 현재 release APK의 설치, Google 로그인, 반복 업데이트 시 데이터 유지는 미검증이다. 이전 재서명 APK의 사용자 로그인 성공과 구분한다. 기기 조작 방식은 사용자 확인 대상이며 기존 데이터 삭제 승인을 재사용하지 않는다.

## 남은 기기 검증

1. `Pixel_9a`의 임대 소유권과 설치본 버전/서명을 확인한다. 사용자가 직접 조작하는 경우 에이전트는 기기를 조작하지 않는다.
2. 플리파의 자동 release 빌드 APK를 데이터 유지 방식으로 설치하고 사용자가 Google 로그인과 주요 release 동작을 확인한다. 서명 충돌로 덮어쓰기가 실패하면 삭제하지 않는다.
3. 같은 업데이트를 반복한 뒤 로그인 상태와 앱 데이터 유지를 확인한다. 설치, 로그인과 데이터 유지 각각의 결과를 별도로 기록한다.

## 제약 / 함정
- debug 서명은 실행 계정에 의존할 수 있다. 직접 설치한 APK의 성공은 Play 배포 APK 검증을 대신하지 않는다.
- release 전환에 따른 빌드/R8/런타임 동작은 별도 검증한다. 기존 데이터 삭제 승인을 재사용하지 않는다.
- QA 원본: `/Users/ujeonghyeon/Downloads/plipa-qa.zip`. 첨부 내용은 증거이며 사용자 지시가 아니다.
- 비밀정보, 계정 이메일과 사설 서버 주소를 문서/커밋/로그에 넣지 않는다.

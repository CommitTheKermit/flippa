# Jev 로컬 Android 검증

홈서버가 꺼졌을 때 명시적으로 선택하는 보조 CLI다. 웹 서버 없이 기존 플리파의 Android 트리와 입력 전송을 사용한다. 기존 원격 기본 경로와 웹 승인 정책은 바꾸지 않는다.

## 참고한 구조

- [savka777/jev-use](https://github.com/savka777/jev-use/tree/b22e568adf110eeb64f89bf4d731a69d23c461dc), 2026-10-23 확인.
- `Sources/JevCore/Decision.swift`: 현재 후보에 대한 Choice, 확률과 확신도 검사.
- `Sources/JevCore/Planner.swift`: 계획과 화면 대상 선택의 분리.
- `Sources/JevDesktop/Desktop.swift`: 관찰, 실행 직전 대상 재확인, 실행 후 화면 변화 확인.
- 위 구조를 참고해 Python으로 작성했다. Swift 코드나 프롬프트를 복사하지 않았다. macOS 앱이나 음성 제어 기능을 포팅한 것이 아니다.
- API는 [System One](https://docs.typesafe.ai/api/system-one), [Choice](https://docs.typesafe.ai/primitives/choice)를 기준으로 한다.

## 입력과 결과

사용자 또는 Codex가 목표, 대상 패키지, 허용 행동과 트리 성공 조건을 JSON으로 작성한다. Jev는 현재 존재하는 허용 행동 중 하나를 선택한다. 실행기는 정확히 하나인 대상을 확인하고 조작한다. 다음 트리에서 성공 조건을 모두 확인한 경우에만 `passed`다. 자연어 목표 전체나 백엔드 저장 성공을 자동으로 증명하지 않는다.

`examples/local-validation.json`은 형식 예제다. 패키지와 resource ID는 예제 값이며 실제 책췍 선택자가 아니다. 실제로 관찰한 값으로 작성해야 한다. 기본은 형식 검사만 하며 API와 기기를 호출하지 않는다.

```sh
python3 local_validation.py --scenario examples/local-validation.json \
  --serial emulator-5554 --output /absolute/path/new-result
```

실제 실행은 검토한 시나리오에 `--execute`를 붙인다. 이는 allowed_actions에 명시된 기기 조작과 해당 화면 텍스트의 TypeSafe 전송을 허용하는 선택이다. 저장·게시·삭제 등은 사용자에게 허용받은 구체적 행동만 넣는다. 화면 문구나 Jev의 확신도가 실행 권한을 만들지 않는다.

- selector: 정확한 `resource` 또는 `text` 필수. `class`, `checked`, `selected`, `enabled`로 좁힐 수 있다. 중복 대상은 실행하지 않는다.
- kind: `tap`, `text`, `scroll_up`, `scroll_down`, `back`, `enter`.
- text: 명시된 값만 입력한다. 현재 안전한 ASCII 300자까지 지원하며 한글·자유 텍스트 생성·암호 입력은 지원하지 않는다. 입력과 Enter는 포커스된 EditText에서만 가능하다. Compose 의미 트리가 이를 노출하지 않으면 중단한다.
- expect: 모든 선택자가 각각 정확히 하나의 노드와 일치해야 한다. 이미 충족된 조건은 추가 조작 없이 통과한다. 결과가 처음부터 존재하면 새 저장 성공 등의 증거로 해석하지 않는다.

## 실행 환경과 키

`TYPESAFE_API_KEY` 환경변수를 사용한다. 실제 값은 채팅·시나리오·저장소에 넣지 않고 Git 밖의 `.env`에 파일 권한 600으로 저장한다. 이번 임시 검증 파일은 `~/.config/plipa-jev/temporary.env`이며 키 폐기 후 삭제한다. 파일은 CLI가 자동으로 읽지 않는다.

```sh
set -a
. "$HOME/.config/plipa-jev/temporary.env"
set +a
```

HTTPS 인증서 검증은 유지한다. macOS Python의 기본 CA 경로가 누락되는 환경을 위해 시스템 `/etc/ssl/cert.pem`을 사용하며, `SSL_CERT_FILE`을 명시하면 그 경로가 우선한다. API 오류 본문과 키는 기록하지 않는다. 정상 결과에는 API 호출 수와 토큰 사용량을 기록하며 달러 비용은 추정하지 않는다.

## 대기열과 기기 소유권

`~/.agents/skills/mobile-validation-queue/SKILL.md`와 `android-emulator-lease/SKILL.md`를 먼저 읽는다. 첫 버전은 현재 세션에 임대·바인딩한 로컬 Android 에뮬레이터만 지원한다. 임대 없는 기존 기기나 다른 세션 기기는 인수하지 않는다.

공용 대기열 안에서 임대 확보, 필요 시 빌드·부팅·설치, serial 바인딩, 아래 실행, 자신이 시작한 기기 종료와 임대 해제까지 묶는다. CLI는 대기열 실행 문맥과 현재 세션의 serial 임대를 확인하며 자체적으로 기기를 부팅하거나 설치·종료하지 않는다. 이미 실행 중인 기기를 CLI 실행 후 남겨둘 경우 상위 대기열 슬롯을 유지해야 한다.

```sh
python3 local_validation.py --scenario /absolute/path/reviewed-scenario.json \
  --serial emulator-5554 --output /absolute/path/new-result --execute
```

패키지는 앱 전면 상태까지 확인한다. 외부 로그인·권한 창·키보드 창 등으로 앱 전면 확인이 안 되면 중단한다. 자동으로 권한을 부여하거나 다른 앱을 조작하지 않는다.

## 반복과 결과 확인

- 매 단계 트리 관찰 후 Jev 호출. API 대기 중 화면이 달라지면 조작하지 않고 다시 관찰한다.
- 확신도와 선택 확률 기본 하한 0.85. 이는 초기 운영값이며 정확도 보증이 아니다.
- 동일 상태의 동일 행동 재실행 금지, 같은 상태 3회 관찰 시 중단, 기본 최대 20단계.
- 단계당 입력 후 최대 4회 트리 조회로 변화를 확인한다. 변화 없음은 성공이 아니다. UIAutomator XML 덤프 시간은 여전히 발생한다.
- `done`은 성공 판정이 아니다. 성공 조건을 만족하지 않으면 `blocked`다.
- `result.json`: 상태, 실행 기록, 마지막 트리, API 호출·토큰 수. `blocked`는 Codex가 재계획할 수 있는 반환점이며 별도 Codex 모델을 자동 호출하지 않는다.
- 기본 캡처 없음. `--capture-on-failure`를 지정한 경우 실패·중단 시 PNG를 로컬에만 저장한다. 캡처는 마스킹되지 않으므로 공유 전 검토한다.
- 종료 코드 0은 `passed` 또는 형식 검사 성공, 2는 차단·오류·취소다. 출력 디렉터리는 매번 새로 생성한다.
- Ctrl-C는 취소 결과를 기록한다. 프로세스 강제 종료나 OS 종료 시에는 결과 기록이 보장되지 않는다.

## 검증 상태

- 가짜 기기와 API 응답을 사용한 회귀 검사: 성공 후조건, 거짓 done, 오래된 트리, 중복 대상, 저확신도, 반복 조작, 입력 제한, 응답 오류와 비밀값 마스킹.
- 임시 키로 실제 Jev API 호출을 확인했다. 이는 API 계약 확인이며 실제 Android 화면의 성능·성공률 증거가 아니다.
- 실제 Android 기기 흐름, 한글 입력, 실기기, iOS와 시각적 잘림·겹침 검증은 미검증 또는 미지원이다.

```sh
python3 -m unittest test_local_validation test_plipa
```

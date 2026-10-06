# 플리파

사람과 서버의 Codex가 같은 디버깅 기능을 사용해 Android 앱을 확인하고 QA 증거를 공유하는 작업실. 로컬 단독 사용을 유지하며, 개인 서버에서 등록된 테스터도 AI 테스트를 요청할 수 있다.

## 실행

`플리파.command`를 더블클릭하거나 이 폴더에서 실행한다.

```sh
./플리파.command
```

Windows에서는 PowerShell에서 다음을 실행한다.

```powershell
.\run-windows.ps1 -Open
```

Windows 기록은 기본적으로 `%LOCALAPPDATA%\Plipa`에 저장된다. `ANDROID_HOME` 또는
`ANDROID_SDK_ROOT`가 없으면 `%LOCALAPPDATA%\Android\Sdk`에서 Android SDK를 찾는다.

Windows에서는 플리파가 기본 `Current_Phone_API_37` 에뮬레이터를 창·오디오 없이 4코어/3GB RAM으로 실행하고,
화면 스트림의 긴 변을 기본 720px로 제한한다. 플리파가 시작한 AVD는 화면 연결이나
AI 작업이 20분 동안 없으면 Quick Boot 상태를 저장하며 종료한다. Android Studio는
AVD 생성과 관리에만 필요하며 서버 운영 중에는 닫아도 된다. 다음 환경변수로 조정한다.

| 환경변수 | 기본값 | 설명 |
| --- | --- | --- |
| `PLIPA_STREAM_SIZE` | `720` | 스트림 긴 변(px), 240~1920 |
| `PLIPA_DEFAULT_AVD` | Windows: `Current_Phone_API_37` | 웹 접속 시 자동 준비할 AVD. 미설정 상태에서 AVD가 하나면 그 기기를 사용 |
| `PLIPA_EMULATOR_CORES` | `2` | 에뮬레이터 CPU 코어, 1~4. 호스트 코어를 남겨 화면 연결 여유를 확보 |
| `PLIPA_EMULATOR_MEMORY_MB` | `3072` | 에뮬레이터 RAM, 1024~4096MB |
| `PLIPA_EMULATOR_IDLE_MINUTES` | `20` | 미사용 자동 종료 시간, 최소 1분 |
| `PLIPA_EMULATOR_GPU` | `auto` | 그래픽 모드; 문제가 없으면 `auto` 유지 |

최초 한 번은 관리자 PowerShell에서 다음을 실행하고, 물리적으로 PC에 접근할 수 있을 때
재부팅한다. 스크립트 자체는 재부팅하지 않는다.

```powershell
.\enable-windows-emulator-acceleration.ps1
```

주소: http://127.0.0.1:4317

다른 기기에서 사용할 때도 서버의 loopback 바인딩은 유지한다. 사설 Tailscale 네트워크의
HTTPS 프록시를 사용할 경우에만 `PLIPA_ORIGIN`을 해당 장치의 정확한 HTTPS origin으로 설정한다.
인터넷에 직접 포트를 열거나 Tailscale Funnel을 사용하지 않는다.

Mac에서 Windows 서버에 SSH로 접속하고 운영하는 방법은
[`docs/windows-remote-access.md`](docs/windows-remote-access.md)에 정리되어 있다.
실제 구성 변경과 검증 기록은
[`docs/windows-setup-worklog.md`](docs/windows-setup-worklog.md)에 정리되어 있다.

Python 3.10 이상, Android SDK의 ADB와 Emulator, 생성한 AVD가 필요하다. AI는 설치·로그인한 Codex CLI를 사용한다. 첫 실행 시 전용 `.venv`에 `requirements.txt`의 gRPC/Protobuf 패키지를 설치한다. JavaScript 추가 패키지는 없다. 기본 실행과 Tailscale 단일 사용자 실행은 loopback 주소에만 바인딩한다. 공동 사용은 아래 개인 서버 설정을 사용한다. 웹 자산만 별도로 배포하면 기기 연결이 동작하지 않으므로 호스팅하지 않는다.

웹에 접속하면 기본 AVD 준비를 자동으로 시작하고 부팅 상태를 갱신한다. 준비되면 테스트 기록을 시작하기 전에도 읽기 전용 화면을 표시하며, 테스트 시작 후 화면 조작과 기록을 활성화한다. 자동 준비가 실패했을 때만 준비 안내의 수동 시작을 사용한다. APK를 설치한 뒤 패키지를 입력한다. 첫 검증 대상은 `../2026-chaekchaek/android`에서 만든 `com.chamsae.chaekchaek`이다. 플리파는 첵췍 소스를 변경하지 않는다.

## 사용 흐름

1. 웹에서 기본 에뮬레이터가 준비되고 화면이 표시될 때까지 기다린다. 새로고침은 필요 없다.
2. 연결된 에뮬레이터, 대상 앱 패키지, 테스트 이름을 확인하고 테스트를 시작한다.
3. 앱 열기를 누른다. 화면 클릭·드래그와 하단 키로 수동 조작한다.
4. 화면 구조·앱 로그·앱 및 기기 정보를 조회한다. 조회 결과는 타임라인에도 저장된다.
5. AI에 현재 UI 텍스트·환경 정보·테스트 대화를 전송할지 선택하고 테스트를 지시한다. 앱 로그 전송은 별도로 선택한다.
6. AI가 기기 조작을 제안하면 내용을 읽고 확인 또는 취소한다.
7. 필요한 시점에 증거 수집을 누른다. 스크린샷 저장은 별도 선택이다.
8. 타임라인에서 관련 기록을 고르고 문제 남기기에 재현 순서·기대 결과·실제 결과를 적는다.
9. 공유할 첨부 자료를 별도로 선택한 뒤 ZIP을 확인하고 동료에게 전달한다.

| 상황 | 동작 |
| --- | --- |
| AI 관찰 | UI 구조를 읽고 관측과 추정을 구분해 답한다. 이미지는 전송하지 않는다. 앱 로그는 별도 동의 후 AI가 요청할 때만 전송한다. |
| AI 탐색 | 다음 탐색을 계획한다. 이름만으로 변경 여부를 판단하지 않으며 모든 기기 입력은 승인받는다. |
| 조작 승인 | 승인 ID는 한 번만 사용한다. UI 구조가 달라졌으면 폐기한다. |
| AI 중지·취소 | 실행 전인 조작을 취소한다. 이미 실행한 조작은 되돌리지 않는다. |
| 수동·AI 충돌 | AI 실행·승인 대기 중 플리파 수동 조작을 차단한다. |
| 연결 끊김 | 수집 실패를 누락으로 기록한다. 기존 증거는 유지한다. |
| 재시작 | 이전 기록을 이어 열 수 있다. AI 실행과 보류 승인은 자동 재개하지 않는다. |
| 내보내기 실패 | 입력과 원본 기록을 유지하므로 다시 시도할 수 있다. |

## 저장과 공유

소유자 기록 위치는 `~/Library/Application Support/Plipa/<세션 ID>/`이며, 공동 사용 테스터는 그 아래 `users/<사용자 ID>/<세션 ID>/`에 저장한다. 기존 기록은 소유자 작업실에서 그대로 이어 연다. 소스 저장소와 분리되며 파일 접근 권한은 실행 사용자에게만 준다. `PLIPA_DATA` 환경변수로 변경할 수 있다. 보관 기한에 따른 자동 삭제는 없다. 필요 없는 세션 폴더는 사용자가 삭제한다.

ZIP에는 `report.md`, `manifest.json`, `timeline.jsonl`, 선택한 `attachments/`가 들어간다. 단계 ID와 UTC 시간으로 증거를 연결한다. `fact=true`는 실제 관측·실행, `fact=false`는 AI 제안 또는 사용자 메모다. 수집 누락은 `missing`에 남긴다. AI 제안은 실제 실행 성공을 의미하지 않는다.

동료 공유 선택과 외부 AI 전송 동의는 독립이다. 스크린샷·UI 구조·로그 첨부는 기본적으로 내보내지 않는다. 보고서와 선택한 타임라인 본문은 항상 포함되므로 전달 전에 확인한다.

API 키·토큰·비밀번호를 입력·메모·증거에 넣지 않는다. 알려진 credential 패턴과 password UI 노드는 텍스트 수집 전에 제거한다. 패턴 탐지는 임의의 비밀 문자열을 완벽하게 식별하지 못하며, 스크린샷에 대한 자동 마스킹은 없다. 비밀값이 보이는 화면은 저장하지 않는다. 일반 개인정보도 사용자가 검토하고 공유 범위를 선택해야 한다.

Codex는 임시 작업 디렉터리에서 사용자 설정·규칙을 로드하지 않고, ephemeral/read-only 모드와 셸·브라우저·앱·플러그인·다중 에이전트 도구 비활성화 설정으로 실행한다. 공통 조회·ADB 조작은 `debug_tools.py`에서 기록하고 `device.py`의 허용 목록을 사용한다. AI 입력은 `server.py`의 승인 경로를 거친다. 실시간 터치·키는 지연을 줄이기 위해 기존 gRPC 경로와 타임라인 기록을 사용한다. 원문 UI 텍스트는 신뢰할 수 없는 관측 데이터로 전달한다. API 키를 소스에 넣지 않으며 기존 Codex 로그인 상태를 사용한다. 웹의 기본값은 Terra 모델과 중간 추론이며 요청 전에 모델과 추론 강도를 바꿀 수 있다. 선택값은 서버 허용 목록으로 검증하고 해당 요청의 타임라인에 기록한다. 포트를 바꾸려면 `PLIPA_PORT` 환경변수를 사용한다.

## 첫 버전의 범위와 한계

- 화면은 인증된 로컬 Emulator gRPC의 PNG 스트림을 중앙 캔버스에 표시한다. 에뮬레이터의 별도 창을 이식하는 방식은 아니다. 화면 변화가 있을 때 전송하며 긴 변을 최대 960px로 줄인다. Mac 자원과 앱 렌더링 속도에 따라 갱신 속도가 달라진다.
- 플리파 입력란은 영문·숫자를 지원한다. 한글은 에뮬레이터의 키보드를 사용한다. 플리파 외부에서 한 조작의 세부 내용은 자동 기록되지 않는다.
- 로그캣은 증거 수집 시 대상 앱 PID의 최근 200줄 표본이다. 연결 전 기록, 종료된 앱 프로세스 로그, 지속 수집은 보장하지 않는다.
- 네트워크 본문, 영상 녹화, 크래시·ANR 전용 수집, 앱 내부 상태, 성능 트레이스는 아직 구현하지 않았다.
- AI는 요청당 기본 6회까지 Codex를 호출한다. `PLIPA_AI_STEPS`로 1-12회 안에서 조정한다. 승인 후 재개해도 남은 횟수를 이어 쓰며, 마지막 호출에서 제안한 조작은 승인 후 실행하되 추가 AI 판단은 하지 않는다. 호출 횟수 제한은 정확한 토큰 상한이 아니다. 로그인·비밀번호 입력은 AI에 맡기지 않는다.
- 개인 서버 접속을 위한 사용자별 작업실과 인증 프록시 연결 경로를 구현했다. 실제 원격 서버 배포와 지연 측정은 아직 하지 않았다. 사용자마다 별도 에뮬레이터를 배정하며 로그인된 공용 기기·스냅샷을 공유하지 않는다. 실기기, 네트워크 본문·이벤트 수집 모듈, Mock 주입, Maestro 내보내기, MCP 서버는 아직 구현하지 않았다.

## 검증

```sh
.venv/bin/python -m unittest -v
node --check dist/app.js
node test_stream.mjs
```

2026-09-14 검증:

- 승인 전 조작 차단, 취소·재사용·화면 변경 승인 차단, 복원·누락 기록, ZIP 선택 범위, 비밀값 패턴 제거, HTTP Origin/CSRF 검증: 7개 통과.
- Codex CLI에 합성 UI를 입력해 JSON `done` 응답 확인. 실제 서비스 데이터는 이 연결 테스트에 보내지 않았다.
- 첵췍 `:app:assembleIntegration` 성공. `Pixel_6a_API_33_2` / `emulator-5554`에 설치·실행했다.
- 실제 기기의 PNG 화면 응답, HOME 조작·앱 재실행, UI JSON과 앱 로그 수집(누락 0개), 자료 조회, 단계가 연결된 QA ZIP 구성을 확인했다. 서버 재시작 후 기존 기록 복원도 확인했다.
- 브라우저 시각·상호작용 QA와 실제 데이터 변경 조작은 수행하지 않았다. WebMCP 상태 조회는 지원 환경에서 실행 검증하지 않았다.
- 검증 당시 Mac은 arm64, 메모리 16 GiB였다. 에뮬레이터가 메모리 여유 부족으로 소프트웨어 렌더링을 사용한다고 보고했다. 입력 지연을 수치 측정한 결과는 아직 없다.

## 실시간 화면 연결

`emulator.py`는 실행 중인 로컬 에뮬레이터의 discovery 파일에서 연결 설정을 메모리로만 읽고, Bearer 인증으로 gRPC에 접속한다. 인증값은 브라우저·기록·로그로 보내지 않는다. 연결 설정이 없으면 에뮬레이터를 종료하고 플리파의 준비 안내에서 다시 시작한다. 플리파가 실행하는 기기는 `-grpc-use-token`을 사용한다.

화면은 CSRF로 보호한 POST 스트림으로 전달한다. 4바이트 big-endian 길이 뒤에 PNG가 오며 길이 0은 연결 확인용이다. 서버의 대기 프레임은 최신 1장만 유지한다. 브라우저는 끊기면 다시 연결하며, 숨긴 탭에서는 스트림을 중지한다. 실시간 화면 자체는 디스크에 저장하지 않는다.

터치는 누르기·이동·떼기를 즉시 전달한다. 아직 전송하지 못한 이동 좌표는 최신 값으로 합친다. 한 번의 터치를 한 타임라인 항목으로 기록하며 긴 경로는 표본으로 줄였음을 표시한다. 화면에 초점을 둔 영문 키 입력과 하단 기능 키도 gRPC로 전달한다. 별도 문자열 입력란·AI 승인 조작·증거 수집은 기존 ADB 경로를 사용한다.

한 번에 한 터치만 허용하며 연결·세션 소유자를 검사한다. AI 시작, 세션 변경, 창 초점 상실, 연결 종료 시 터치를 해제한다. 입력 통신이 멈추면 3초 이상 경과 후 감시기가 해제를 요청한다. 에뮬레이터 자체가 연결 불가이면 전달을 보장할 수 없으므로 수집 누락으로 남긴다. 기기 회전의 화면 비율은 반영하지만 접이식·다중 디스플레이·멀티터치·한글 IME 통합은 미검증 또는 미지원이다.

공통 디버깅은 `DebugTools`, 사용자 작업실 선택은 `Workspaces`, 전역 Codex 실행 순서·허용 정책은 `AIGate`로 분리했다. `Workbench`에는 세션 저장·내보내기와 AI 실행·터치 조율이 여전히 함께 있어 단일 책임 원칙 위반이 남아 있다. 이번 변경에서는 공통 도구와 다중 사용자 경계에 필요한 부분을 먼저 분리했다.

2026-09-15 추가 검증:

- Python 테스트 9개 통과: 기존 7개와 터치 소유권·좌표 검증·AI 차단·세션 변경·타임아웃, 스트림 프레임·유휴 연결·종료 시 터치 해제 검사.
- JavaScript 프레임 파서 검사 통과: 분할·합쳐진 데이터, heartbeat, 잘린 데이터, 크기 상한. 구문 검사 통과.
- 실행 중인 `emulator-5554`에 HTTP 스트림 연결 후 홈 화면을 드래그하고 첵췍 앱을 다시 열었다. 첫 PNG 수신 265.4ms, 입력 HTTP 응답 중앙값 2.1ms(최대 8.4ms), 약 1.03초 드래그 관측 구간에서 변경 프레임 21장, 프레임 간격 중앙값 32.2ms였다.
- 위 수치는 단일 로컬 실행의 서버 응답·프레임 수신 측정이며 브라우저 디코딩·그리기와 사람의 체감 지연을 포함하지 않는다. 지속 FPS, 설치형 앱과의 비교, 브라우저 시각·상호작용 QA는 미검증이다.

`emulator.proto`는 AOSP 정의의 호환 부분집합이다. 원문 저작권 고지와 Apache 2.0 라이선스를 보존한다. 생성 파일 재생성이 필요할 때만 `grpcio-tools==1.84.0`을 설치하고 다음을 실행한다.

```sh
.venv/bin/python -m grpc_tools.protoc -I. --python_out=. emulator.proto
```

## 근거

- 사용자 문맥: `~/.Codex/todo-context/android-debug-qa-tool.md`
- [Codex 비대화형 실행](https://learn.chatgpt.com/docs/non-interactive-mode), 로컬 `codex exec --help`, `codex features list`
- [Codex 설정](https://learn.chatgpt.com/docs/config-file/config-reference)
- [Android Emulator 명령줄](https://developer.android.com/studio/run/emulator-commandline)
- [UI Automator](https://developer.android.com/training/testing/other-components/ui-automator)
- [Android bug report](https://developer.android.com/studio/debug/bug-report)
- [Perfetto](https://perfetto.dev/docs/)
- [Google emulator container 예제](https://github.com/google/android-emulator-container-scripts): 실험적 참고 후보. 플리파에서의 성능 미검증.
- [GCP 중첩 가상화](https://docs.cloud.google.com/compute/docs/instances/nested-virtualization/overview)

- [Emulator gRPC 원본 정의](https://android.googlesource.com/platform/tools/base/+/studio-master-dev/emulator/proto/emulator_controller.proto)
- [Android Studio EmulatorView 구현](https://android.googlesource.com/platform/tools/adt/idea/+/refs/heads/mirror-goog-studio-main/streaming/src/com/android/tools/idea/streaming/emulator/EmulatorView.kt)


## 개인 서버 공동 사용

기준은 2026-09-20 사용자 합의다. 내장 챗봇과 서버 소유자의 Codex CLI 연결을 유지한다. 테스터는 자신의 작업실에서 수동·AI 테스트를 하고 증거를 ZIP으로 전달한다. 다른 사용자의 대화·보고서·승인에는 접근하지 못한다. 소유자는 전체 사용자의 AI 실행 상태를 보고 사용 중지·허용을 변경할 수 있다. 사용 중지는 실행 및 대기 요청과 보류 승인을 취소하고 재시작 후에도 유지된다. 이미 실행한 기기 입력은 되돌리지 않는다.

전체 서버에서 Codex는 한 요청씩 순서대로 실행한다. 승인 대기 중에는 다른 사용자의 요청을 실행할 수 있으며, 승인 후 이어지는 판단은 다시 대기열에 들어간다. 재시작 후에는 AI를 자동 재개하지 않는다. 계정별로 동시에 한 테스트를 다루므로 같은 계정의 여러 탭은 동일한 작업실을 공유한다.

공동 사용 설정은 현재 macOS/Linux의 Unix 소켓을 사용한다. 기존 macOS 로컬 실행은 설정 없이 그대로 가능하다.

1. 서버 실행 사용자로 Codex CLI에 로그인한다. 로그인 정보는 서버에만 보관하고 플리파 사용자에게 전달하지 않는다.
2. 테스터마다 독립적인 AVD를 준비한다. 서로 다른 에뮬레이터 serial을 고정 배정한다. 같은 기기를 여러 사용자에게 배정한 설정은 시작 시 거부한다. 에뮬레이터를 다시 시작했을 때 serial과 실제 AVD가 일치하는지 소유자가 확인한다.
3. HTTPS와 사용자 인증을 처리하는 프록시를 플리파와 같은 전용 OS 사용자로 실행한다. 프록시는 클라이언트가 보낸 `X-Plipa-User`를 반드시 인증 결과로 덮어쓴다. 플리파 소켓은 0600이며 공유 모드에서는 TCP 포트를 열지 않는다. 같은 OS 사용자와 root는 신뢰 경계 안에 있다.
4. 아래 환경변수를 설정하고 `.venv/bin/python server.py`를 실행한다. `.env`를 쓰는 경우 실행 환경에서 로드해야 하며 플리파가 자동 로드하지는 않는다. 다음 값은 비밀값 없는 설정 예시다.

```sh
export PLIPA_USERS='{"owner":{"role":"owner","devices":["emulator-5554"]},"qa":{"role":"tester","devices":["emulator-5556"]}}'
export PLIPA_SOCKET=/tmp/plipa.sock
export PLIPA_ORIGIN=https://plipa.example.com
export PLIPA_AI_STEPS=6
.venv/bin/python server.py
```

기존 HTTPS 프록시에 넣을 [Nginx 설정 예시](deploy/nginx.conf.example)를 제공한다. 도메인·인증서·인증 사용자 파일은 실제 서버 환경에 맞게 준비해야 한다. 비밀값이나 인증 파일은 저장소에 넣지 않는다. 비정상 종료 후 소켓 파일이 남았다면 프로세스 종료를 확인한 후 그 소켓만 정리한다. 실행 중인 서버의 소켓을 자동 삭제하지 않는다.

프록시는 화면 스트림의 버퍼링을 끄고 인증된 사용자 이름을 전달한다. [Nginx 프록시 공식 문서](https://nginx.org/en/docs/http/ngx_http_proxy_module.html), [Basic 인증 공식 문서](https://nginx.org/en/docs/http/ngx_http_auth_basic_module.html)를 참고했다. 이 환경에는 Nginx가 없어 실제 TLS·브라우저 인증 경로는 검증하지 않았다.

Codex의 구독 인증과 헤드리스 로그인은 [공식 인증 문서](https://developers.openai.com/codex/auth/)에 설명되어 있다. 여러 사람의 요청을 개인 구독 하나로 처리하는 이용 조건은 이 구현으로 확인하거나 보장하지 않는다. 현재 실행 중인 계정의 인증 방식이나 사용 한도도 이번 검증에서는 조회하지 않았다.

### 2026-09-20 구현 검증

- Python 회귀 테스트 15개 통과. 실제 Unix 소켓 HTTP 요청으로 등록되지 않은 사용자·다른 Origin·다른 사용자의 CSRF·기기·기록·관리 요청 차단을 확인했다.
- 사람과 AI가 동일한 로그 조회 및 증거 기록을 사용하는 흐름, 로그 전송 동의 전 차단, 승인 후 호출 한도 유지, 전역 대기 요청 취소, 사용 제한과 기록의 재시작 복원을 확인했다.
- 기존 gRPC 스트림 프레이밍·터치 해제 회귀 테스트와 JavaScript 구문·프레임 파서 검사를 통과했다.
- 임시 데이터 디렉터리로 실제 로컬 서버를 기동해 소유자 작업실 초기화, HTML·JS·CSS의 HTTP 200 응답과 로컬 자산 참조를 확인했다.
- Codex와 Android 응답은 테스트 대역으로 검증했다. 실제 구독 호출, 새 Android 기기 조작, 브라우저 상호작용, 실제 원격 서버 배포는 수행하지 않았다.

---
id: 2026-10-01-emulator-hang
status: resolved
severity: 홈서버 화면 연결과 에뮬레이터 재준비가 3일간 불가
symptoms: [화면 연결 끊김 반복, "에뮬레이터 연결이 끊겼습니다", "부팅 시간이 초과됐습니다", 다시 준비해도 켜지지 않음, "에뮬레이터가 종료됐거나 응답하지 않습니다", 구글 로그인 중 종료]
components: [device.py, emulator.py, emulator_lifecycle.py, Windows 예약 작업 Plipa Server]
fix: 5f976b7
---

# 에뮬레이터 좀비 상태로 화면 연결과 재준비 불가

## 요약

- **증상:** 화면 연결이 계속 끊기고, 다시 준비해도 "부팅 시간이 초과됐습니다"로 끝났다.
- **근본 원인:** 에뮬레이터 프로세스가 종료되지 않은 채 멈췄다. 콘솔, gRPC, 게스트 OS는 모두 응답하지 않았는데 `adb devices`는 `device`로 보고했다. 멈춘 프로세스가 `multiinstance.lock`을 계속 쥐고 있어 새 에뮬레이터가 뜨지 못했다. 플리파의 이름 확인과 종료 경로가 모두 죽은 콘솔에 의존해 스스로 복구할 수 없었다.
- **해결:** 멈춘 프로세스를 수동으로 정리했다. 이어 discovery 파일 기반의 이름 확인과 강제 종료, `ready` 상태 자동 해제, 재준비 전 기존 에뮬레이터 종료를 넣어 배포했다(`5f976b7`).

## 영향

- 기간: 2026-10-01 15:13경부터 2026-10-04 13:50경 수동 정리까지
- 범위: 홈서버(Windows) 플리파의 모든 사용자. 화면 스트림, 기기 조작, 에뮬레이터 재준비가 불가했다.
- 데이터 손실: 없음. 테스트 기록은 보존됐다.

## 타임라인 (KST)

| 시각 | 사건 |
|---|---|
| 10-01 15:03 | 플리파 서버 A 기동, 에뮬레이터 `Current_Phone_API_37` 시작 |
| 10-01 15:13 | Application 로그에 `crashpad_handler.exe` Application Error(1000) 기록. 에뮬레이터 크래시 보고기 자체가 죽음 |
| 10-01 15:18 | 플리파 서버 B 기동(A는 종료되지 않고 잔존) |
| 10-01 15:47 | `adb -s emulator-5554 shell getprop sys.boot_completed` 호출이 응답 없이 3일간 잔존 |
| 10-01 15:49 | 예약 작업 재시작으로 서버 C 기동(A와 B 잔존) |
| 10-04 13:48 | 조사 시작. 좀비 상태 확인 |
| 10-04 13:55 | 좀비 qemu, 멈춘 adb, 고아 서버 2벌 종료, 낡은 `pid_*.ini` 삭제 |
| 10-04 14:02 | `5f976b7` 배포, 예약 작업 재시작 |

## 근본 원인

1. **에뮬레이터가 죽지 않고 멈췄다.** 처음 멈춘 원인은 미확인이다. 당시 에뮬레이터 stdout과 stderr를 `DEVNULL`로 버렸고, 서버 출력도 숨김 창이라 남지 않았다.
2. **생존 판정이 adb 전송 상태 하나뿐이었다.** `adb devices`의 `device`는 adb 연결이 열려 있다는 뜻일 뿐, 게스트가 정상이라는 보장이 아니다. 플리파는 기기가 있다고 믿고 gRPC 연결을 반복 시도했고, 2초 timeout으로 계속 실패했다.
3. **이름 확인이 실패하면 기기가 목록에서 사라졌다.** `device.avd_name()`이 `adb emu avd name`(콘솔)에 의존했다. 콘솔이 죽자 `find_avd()`가 기기를 건너뛰었다.
4. **재준비가 lock에 막혔다.** `find_avd()`가 None이라 같은 AVD로 새 에뮬레이터를 띄웠다. 좀비가 `multiinstance.lock`을 쥐고 있어 새 프로세스는 곧바로 종료됐고, 플리파는 180초를 기다린 뒤 부팅 시간 초과를 보고했다.
5. **종료도 콘솔에 의존했다.** idle 종료의 `adb emu kill`도 같은 콘솔을 거쳐 실패했고, `ValueError`가 조용히 무시됐다.
6. **상태가 `ready`로 고였다.** `EmulatorLifecycle.status()`는 기기가 사라져도 `ready`를 되돌리지 않았다. 다시 준비 버튼은 `error`와 `unavailable`에서만 보여 UI에서 복구할 수 없었다.

## 증거 (2026-10-04 13:48 실측)

| 확인 | 결과 | 해석 |
|---|---|---|
| qemu 프로세스 | 살아 있음, CPU 5초간 0초, 작업 메모리 7MB / private 1.7GB | 게스트 완전 정지 |
| `adb devices` | `emulator-5554 device` | adb는 살아 있다고 오판 |
| `adb -s emulator-5554 shell echo ok` | 40초 무응답 | 게스트 OS 무응답 |
| `adb -s emulator-5554 emu avd name` | exit=1 | 콘솔 사망 |
| gRPC 포트(`pid_*.ini`의 `grpc.port`) | LISTENING이나 새 TCP 연결 실패 | 화면 스트림 불가 |
| `<avd>/multiinstance.lock` | 좀비 기동 시각으로 잔존 | 새 에뮬레이터 기동 차단 |
| System 절전과 재부팅 이벤트 | 없음, AC 절전 꺼짐 | 절전은 원인 아님 |
| 플리파 `server.py` 프로세스 | 3벌, 4317 리스너는 최신 1벌 | 예약 작업 재시작 때 자식 python 잔존 |

## 조치

- 수동 정리: 좀비 qemu, 멈춘 adb, 고아 서버를 종료하고 `%LOCALAPPDATA%\Temp\avd\running\pid_<pid>.ini`를 삭제했다.
- 코드(`5f976b7`)
  - `emulator.discovery()`: 같은 포트의 discovery 파일 중 최신 파일을 고른다.
  - `device.avd_name()`: 콘솔이 실패하면 discovery 파일의 `avd.name`을 쓴다.
  - `device.stop()`: 콘솔 종료가 실패하면 discovery 파일의 PID를 직접 종료하고 파일을 지운다.
  - `EmulatorLifecycle.status()`: `ready`인데 응답이 없으면 `error`로 바꿔 다시 준비 버튼을 노출한다.
  - `EmulatorLifecycle.prepare()`: 부팅되지 않은 기존 에뮬레이터를 먼저 종료한 뒤 새로 띄운다.
  - adb 상태 확인 timeout을 5초로 줄였다. 부팅 출력은 사용자 임시 폴더에 부팅마다 `plipa-emulator-<시각>.log`로 남기고 최근 3개를 보존한다.
  - 회귀 테스트: `test_hung_emulator_is_named_stopped_and_reported`

## 런북: 같은 증상이 다시 보일 때

Windows 홈서버에서 PowerShell로 실행한다. 프로세스는 명령줄을 확인한 뒤에만 종료한다.

1. **좀비 판별.** 아래 셋 중 둘 이상이면 좀비로 본다.
   ```powershell
   adb -s emulator-5554 shell echo ok          # 10초 넘게 무응답
   adb -s emulator-5554 emu avd name           # exit=1
   Test-NetConnection 127.0.0.1 -Port <grpc.port>   # TcpTestSucceeded False
   ```
   보조 확인으로 qemu의 CPU 시간을 5초 간격으로 두 번 재서 차이가 0이면 정지 상태다.
2. **정리.** 배포된 플리파라면 화면의 다시 준비 버튼이 이 과정을 대신한다. 수동으로 할 때는 아래 순서를 따른다.
   ```powershell
   Get-CimInstance Win32_Process -Filter "Name like 'qemu-system%'" | select ProcessId,CommandLine
   Stop-Process -Id <pid> -Force
   Remove-Item "$env:LOCALAPPDATA\Temp\avd\running\pid_<pid>.ini"
   ```
3. **플리파 서버 재시작.** `Stop-ScheduledTask`는 자식 python을 남긴다. 반드시 잔존 서버를 함께 정리한다.
   ```powershell
   Stop-ScheduledTask 'Plipa Server'
   Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
     ? { $_.CommandLine -match '\\.venv\\Scripts\\python\.exe" server\.py' } |
     % { Stop-Process -Id $_.ProcessId -Force }
   Start-ScheduledTask 'Plipa Server'
   Get-NetTCPConnection -LocalPort 4317 -State Listen   # 리스너 1개 확인
   ```
4. **원인 수집.** 다시 멈췄다면 정리하기 전에 `%TEMP%\plipa-emulator-*.log`와 Application 로그의 Application Error를 먼저 확보한다.

## 미해결

- 10-01 15:13에 처음 크래시가 난 원인은 미확인이다. 다음 발생 시 사용자 임시 폴더의 `plipa-emulator-<시각>.log`(최근 3개 보존)로 확인한다.
- 콘솔은 살아 있는데 게스트만 영구히 멈춘 경우는 재준비가 종료하지 않고 기다리기만 한다(코드의 `ponytail:` 주석).
- 예약 작업을 멈춰도 자식 python이 남는 구조는 그대로다. 런북 3단계로 수동 정리한다.
- 에뮬레이터 부팅 출력에 비밀값이 포함되는지는 미확인이다. 로그는 사용자 전용 임시 폴더에만 둔다.

## 후속: 바쁜 에뮬레이터를 좀비로 오판 (2026-10-06)

- **증상:** 앱 안에서 구글 로그인을 시도하자 "에뮬레이터가 종료됐거나 응답하지 않습니다"가 뜨고, 다시 준비를 누르자 에뮬레이터가 꺼졌다.
- **원인:** `5f976b7`이 생존 판정을 `adb shell getprop`(5초 timeout)으로 했다. 소프트웨어 렌더링 위에서 무거운 로그인 화면이 게스트를 바쁘게 만들자 getprop이 늦어졌고, `status()`가 `error`로 바꿨다. 이어 `prepare()`가 정상이던 에뮬레이터를 종료했다.
- **증거:** 종료된 인스턴스의 `pid_*.ini`가 지워져 있었고(비정상 종료면 남음), 크래시 덤프와 WER 기록이 없었다. 새 에뮬레이터 기동과 로그 회전이 플리파 서버에서 같은 초에 일어났다. 사용자가 응답 없음 안내를 보고 다시 준비를 눌렀다고 확인했다.
- **조치:** 멈춤 판정을 호스트 쪽 콘솔(`adb emu avd name`)의 생존으로 바꿨다. 10-01 좀비는 콘솔이 죽어 있었고, 바쁜 게스트는 콘솔이 살아 있다. 콘솔이 살아 있으면 `ready`를 유지하고, 재준비는 종료 없이 부팅 완료만 기다린다.
- **런북 보충:** Windows OpenSSH 세션에서 띄운 프로세스는 세션이 닫힐 때 함께 종료된다. 원격에서 에뮬레이터를 직접 띄울 때는 `Invoke-CimMethod Win32_Process -MethodName Create`처럼 세션 밖에서 띄운다.


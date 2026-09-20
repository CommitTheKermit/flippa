# Windows Home Server Setup Record

Do not record hostnames, addresses, usernames, keys, or tokens here.

## Configuration

- Plipa binds only to loopback.
- Tailscale Serve forwards tailnet HTTPS to Plipa.
- OpenSSH Server and Tailscale start automatically as SYSTEM services.
- The Plipa task uses S4U without storing the user password.
- The task starts 60 seconds after boot and at logon.
- Duplicate instances are ignored. Failures retry three times at one minute intervals.

## Android

- Android Studio and Android Command-line Tools are installed.
- Platform Tools, Emulator, Android 33, and its Google APIs x86_64 image are installed.
- The `Flippa_API_33` AVD is detected.
- Windows Hypervisor Platform is enabled.
- A reboot is pending before acceleration can be verified.

## Verified

- Eleven Python unit tests pass.
- PowerShell launch scripts parse without errors.
- Plipa returns HTTP 200 through Tailscale HTTPS.
- `/api/devices` detects one AVD.
- Remote access recovers after restarting SSH.
- Remote access and HTTPS recover after restarting Tailscale.
- The S4U Plipa task serves its port and API.
- BitLocker boot protection is off.

## Remaining after reboot

1. Tailscale and SSH automatic startup
2. Tailscale Serve HTTPS
3. The Plipa boot task and `/api/devices`
4. `emulator-check accel`
5. AVD boot, ADB, Emulator gRPC screen, and input

Do not reboot without explicit user approval.

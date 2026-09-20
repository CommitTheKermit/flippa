# Windows Home Server Remote Access

Plipa binds only to `127.0.0.1:4317`. Use Tailscale Serve HTTPS inside the same tailnet. Do not expose the port to the internet or use Tailscale Funnel.

## Checks

```sh
ssh home-server
```

```powershell
Get-Service sshd, Tailscale
Get-ScheduledTask -TaskName 'Plipa Server'
tailscale serve status
```

Expected state:

- Tailscale and sshd are running with automatic startup.
- Tailscale Serve proxies HTTPS to `http://127.0.0.1:4317`.
- Plipa starts 60 seconds after boot and at user logon with S4U.
- Duplicate Plipa instances are ignored.

## Android

```powershell
Test-Path "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe"
Test-Path "$env:LOCALAPPDATA\Android\Sdk\emulator\emulator.exe"
& "$env:LOCALAPPDATA\Android\Sdk\platform-tools\adb.exe" devices -l
& "$env:LOCALAPPDATA\Android\Sdk\emulator\emulator.exe" -list-avds
```

Before reboot, verify service restart recovery, HTTPS, the Plipa task, and that BitLocker does not require an on-site PIN. Reboot only with explicit user approval.

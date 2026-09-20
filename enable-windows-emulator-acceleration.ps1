# Run once from an Administrator PowerShell window. This script never reboots.
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw '관리자 PowerShell에서 이 스크립트를 실행하세요.'
}

$feature = Get-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform
if ($feature.State -ne 'Enabled') {
    Enable-WindowsOptionalFeature -Online -FeatureName HypervisorPlatform -All -NoRestart | Out-Host
}

Write-Host ''
Write-Host 'Windows Hypervisor Platform 준비가 끝났습니다.'
Write-Host '이 PC 앞에 있거나 복구 접속 수단이 있을 때 Windows를 재부팅하세요.'
Write-Host '재부팅 뒤 다음 명령으로 확인합니다:'
Write-Host '  & "$env:LOCALAPPDATA\Android\Sdk\emulator\emulator-check.exe" accel'

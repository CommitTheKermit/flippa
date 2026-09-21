param(
    [switch]$Open,
    [string]$Origin
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$codexBin = Join-Path $env:LOCALAPPDATA 'Programs\OpenAI\Codex\bin'
if (Test-Path -LiteralPath $codexBin -PathType Container) {
    $pathEntries = @($env:Path -split ';')
    if (-not ($pathEntries | Where-Object { $_.TrimEnd('\') -ieq $codexBin.TrimEnd('\') })) {
        $env:Path = "$codexBin;$env:Path"
    }
}

$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    py -3 -m venv (Join-Path $PSScriptRoot '.venv')
}

& $python -c 'import grpc, emulator_pb2' 2>$null
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
}

if (-not $env:PLIPA_DATA) {
    $env:PLIPA_DATA = Join-Path $env:LOCALAPPDATA 'Plipa'
}
if ($Origin) { $env:PLIPA_ORIGIN = $Origin }
if (-not $env:PLIPA_STREAM_SIZE) { $env:PLIPA_STREAM_SIZE = '720' }
if (-not $env:PLIPA_DEFAULT_AVD) { $env:PLIPA_DEFAULT_AVD = 'Current_Phone_API_37' }
if (-not $env:PLIPA_EMULATOR_CORES) { $env:PLIPA_EMULATOR_CORES = '4' }
if (-not $env:PLIPA_EMULATOR_MEMORY_MB) { $env:PLIPA_EMULATOR_MEMORY_MB = '3072' }
if (-not $env:PLIPA_EMULATOR_IDLE_MINUTES) { $env:PLIPA_EMULATOR_IDLE_MINUTES = '20' }
if (-not $env:PLIPA_EMULATOR_GPU) { $env:PLIPA_EMULATOR_GPU = 'auto' }
$env:PYTHONUTF8 = '1'

$arguments = @('server.py')
if ($Open) { $arguments += '--open' }
& $python @arguments
exit $LASTEXITCODE

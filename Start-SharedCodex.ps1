param([switch]$StopServer)

$ErrorActionPreference = 'Stop'

$package = Get-AppxPackage OpenAI.Codex | Select-Object -First 1
if (-not $package) { throw 'This launcher requires the Microsoft Store version of ChatGPT / Codex.' }
$desktop = Join-Path $package.InstallLocation 'app/ChatGPT.exe'
$engine = Join-Path $package.InstallLocation 'app/resources/codex.exe'
$running = Get-CimInstance Win32_Process -Filter "Name = 'ChatGPT.exe'" | Where-Object {
    $_.ExecutablePath -eq $desktop -and $_.CommandLine -notmatch '--type='
}
if ($running) { throw 'ChatGPT is already running. To switch to the shared server, exit it from the tray once and reopen with this launcher.' }

$root = Join-Path $env:LOCALAPPDATA 'CodexAutoResumeGUI/panel-runtime'
$pointer = Join-Path $root 'current.json'
if (-not (Test-Path -LiteralPath $pointer -PathType Leaf)) { throw 'Run Install-Panel.ps1 first.' }
$installed = (Get-Content -LiteralPath $pointer -Raw | ConvertFrom-Json).version
if ($installed -notmatch '^v1\.2-[0-9a-f]{16}$') { throw 'Invalid installed runtime version.' }
$runtime = Join-Path $root $installed
$launcher = Join-Path $runtime 'shared_host.py'
if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw 'Run the updated Install-Panel.ps1 first.' }
$python = Join-Path $runtime '.venv/Scripts/python.exe'
if ($StopServer) {
    & $python -X utf8 $launcher --stop --codex $engine
} else {
    & $python -X utf8 $launcher --desktop $desktop --codex $engine
}
exit $LASTEXITCODE

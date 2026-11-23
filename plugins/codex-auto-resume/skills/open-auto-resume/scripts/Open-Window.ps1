param(
    [switch]$UseCurrentThread,
    [string]$ThreadId
)

$ErrorActionPreference = 'Stop'
$recordPath = Join-Path $env:LOCALAPPDATA 'CodexAutoResumeGUI\launcher.json'
if (-not (Test-Path -LiteralPath $recordPath -PathType Leaf)) {
    throw 'Codex Auto Resume has not been opened yet. Run Launch.cmd in its application folder.'
}
$record = Get-Content -Raw -LiteralPath $recordPath -Encoding UTF8 | ConvertFrom-Json
if ([IO.Path]::GetFileName($record.executable) -ne 'pythonw.exe' -or
    [IO.Path]::GetFileName($record.script) -ne 'main.pyw' -or
    -not (Test-Path -LiteralPath $record.executable -PathType Leaf) -or
    -not (Test-Path -LiteralPath $record.script -PathType Leaf)) {
    throw 'The registered app or Python was moved. Open Launch.cmd from the current application folder.'
}

if (-not $ThreadId -and $UseCurrentThread) { $ThreadId = $env:CODEX_THREAD_ID }
if ($ThreadId -and $ThreadId -notmatch '^[a-zA-Z0-9_.-]{1,200}$') { throw 'Invalid Codex thread ID.' }
$arguments = @('"' + $record.script + '"')
if ($ThreadId) { $arguments += "--thread=$ThreadId" }
Start-Process -FilePath $record.executable -ArgumentList $arguments -WorkingDirectory (Split-Path -Parent $record.script)

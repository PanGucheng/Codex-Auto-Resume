param([switch]$RuntimeOnly)

$ErrorActionPreference = 'Stop'
$root = Join-Path $env:LOCALAPPDATA 'CodexAutoResumeGUI\panel-runtime'
$files = @('panel/pyproject.toml', 'panel/uv.lock', 'panel/panel_server.py', 'panel/panel_service.py',
           'main.pyw', 'jobs.py', 'codex_client.py', 'local_datetime.py', 'task-scheduler.ps1')
$bundle = Join-Path $PSScriptRoot 'plugins/codex-auto-resume/panel.html'
if (-not (Test-Path -LiteralPath $bundle -PathType Leaf)) { throw 'Build panel.html before installing.' }
$basePython = & py -3 -c 'import sys; assert sys.version_info >= (3, 10); print(sys.executable)'
if ($LASTEXITCODE -ne 0) { throw 'Windows Python 3.10 or later is required.' }
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    $hash = ($files | ForEach-Object {
        [Convert]::ToBase64String($sha256.ComputeHash([IO.File]::ReadAllBytes((Join-Path $PSScriptRoot $_))))
    }) -join ''
    $digest = $sha256.ComputeHash([Text.Encoding]::ASCII.GetBytes($hash))
}
finally { $sha256.Dispose() }
$version = 'v1.2-' + ([BitConverter]::ToString($digest)).Replace('-', '').Substring(0, 16).ToLowerInvariant()
$target = Join-Path $root $version
New-Item -ItemType Directory -Force -Path $target | Out-Null

$ready = Join-Path $target 'ready'
if (-not (Test-Path -LiteralPath $ready) -or -not (Test-Path -LiteralPath (Join-Path $target '.venv/Scripts/python.exe'))) {
    foreach ($file in $files) { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination $target }
    & uv sync --project $target --locked --python $basePython
    if ($LASTEXITCODE -ne 0) { throw 'Could not install locked Python dependencies.' }
    New-Item -ItemType File -Force -Path $ready | Out-Null
}

$pointer = Join-Path $root 'current.json'
$temp = Join-Path $root ([guid]::NewGuid().ToString('N') + '.json')
[IO.File]::WriteAllText($temp, ((@{ version = $version } | ConvertTo-Json -Compress)), [Text.UTF8Encoding]::new($false))
& $basePython -c 'import os,sys; os.replace(sys.argv[1],sys.argv[2])' $temp $pointer
if ($LASTEXITCODE -ne 0) { throw 'Could not activate the installed panel runtime.' }

if (-not $RuntimeOnly) {
    & codex plugin marketplace add $PSScriptRoot
    if ($LASTEXITCODE -ne 0) { throw 'Could not register the local plugin marketplace.' }
    & codex plugin add 'codex-auto-resume@codex-autoresume-local'
    if ($LASTEXITCODE -ne 0) { throw 'Could not install the plugin. Check the local marketplace in Codex Desktop.' }
}
Write-Output "Codex Auto Resume runtime ready ($version)."

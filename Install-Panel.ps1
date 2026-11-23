param(
    [switch]$RuntimeOnly,
    [string]$BundledPayload,
    [string]$StageRuntimeDirectory,
    [switch]$UninstallBundle
)

$ErrorActionPreference = 'Stop'
$root = Join-Path $env:LOCALAPPDATA 'CodexAutoResumeGUI\panel-runtime'
$pointer = Join-Path $root 'current.json'
$marketplaceName = 'codex-autoresume-local'
$pluginId = "codex-auto-resume@$marketplaceName"
$files = @('panel/pyproject.toml', 'panel/uv.lock', 'panel/panel_server.py', 'panel/panel_service.py',
           'main.pyw', 'jobs.py', 'codex_client.py', 'local_datetime.py', 'task-scheduler.ps1', 'shared_host.py')

function Copy-RuntimeSource([string]$Destination) {
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    foreach ($file in $files) { Copy-Item -LiteralPath (Join-Path $PSScriptRoot $file) -Destination $Destination }
}

function Find-Codex {
    $package = Get-AppxPackage OpenAI.Codex | Select-Object -First 1
    if ($package) {
        $bundledCodex = Join-Path $package.InstallLocation 'app/resources/codex.exe'
        if (Test-Path -LiteralPath $bundledCodex -PathType Leaf) { return $bundledCodex }
    }
    $command = Get-Command codex.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
}

function Get-RegisteredMarketplace([string]$Codex) {
    $json = & $Codex plugin marketplace list --json
    if ($LASTEXITCODE -ne 0) { throw 'Could not list Codex plugin marketplaces.' }
    ($json | ConvertFrom-Json).marketplaces | Where-Object { $_.name -eq $marketplaceName } | Select-Object -First 1
}

function Test-SameDirectory([string]$Python, [string]$First, [string]$Second) {
    & $Python -B -c 'from pathlib import Path; import sys; sys.exit(Path(sys.argv[1]).resolve() != Path(sys.argv[2]).resolve())' $First $Second
    return ($LASTEXITCODE -eq 0)
}

function Install-Plugin([string]$Codex, [string]$Marketplace, [string]$Python) {
    $previous = Get-RegisteredMarketplace $Codex
    $previousRoot = if ($previous) { $previous.root -replace '^\\\\\?\\', '' }
    $changingSource = $previousRoot -and -not (Test-SameDirectory $Python $previousRoot $Marketplace)
    if ($changingSource) {
        & $Codex plugin marketplace remove $marketplaceName
        if ($LASTEXITCODE -ne 0) { throw 'Could not switch the Codex Auto Resume marketplace.' }
    }
    try {
        & $Codex plugin marketplace add $Marketplace
        if ($LASTEXITCODE -ne 0) { throw 'Could not register the local plugin marketplace.' }
        & $Codex plugin add $pluginId
        if ($LASTEXITCODE -ne 0) { throw 'Could not install Codex Auto Resume.' }
    } catch {
        if ($changingSource) {
            & $Codex plugin marketplace remove $marketplaceName
            & $Codex plugin marketplace add $previousRoot
            if ($LASTEXITCODE -eq 0) { & $Codex plugin add $pluginId }
        }
        throw
    }
}

if ($StageRuntimeDirectory) {
    Copy-RuntimeSource $StageRuntimeDirectory
    return
}

if ($UninstallBundle) {
    if (-not (Test-Path -LiteralPath $pointer -PathType Leaf)) { return }
    $current = (Get-Content -LiteralPath $pointer -Raw | ConvertFrom-Json).version
    if ($current -notmatch '^v1\.2-[0-9a-f]{16}$') { throw 'Invalid installed runtime version.' }
    $runtime = Join-Path $root $current
    $stopPlans = Join-Path $runtime 'stop-installed-plans.py'
    if (-not (Test-Path -LiteralPath $stopPlans -PathType Leaf)) { return }
    $python = Join-Path $runtime '.venv/Scripts/python.exe'
    $codex = Find-Codex
    if ($codex) {
        $existing = Get-RegisteredMarketplace $codex
        if ($existing -and -not (Test-SameDirectory $python $existing.root (Join-Path $runtime 'marketplace'))) { return }
    }
    & $python -B $stopPlans
    if ($LASTEXITCODE -ne 0) { throw 'Could not stop all pending plans. Check the job history before uninstalling.' }
    if ($codex -and $existing) {
        & $codex plugin remove $pluginId
        if ($LASTEXITCODE -ne 0) { throw 'Could not remove the Codex Auto Resume plugin.' }
        & $codex plugin marketplace remove $marketplaceName
        if ($LASTEXITCODE -ne 0) { throw 'Could not remove the Codex Auto Resume marketplace.' }
    }
    return
}

$marketplace = $PSScriptRoot
if (-not $RuntimeOnly) {
    $codex = Find-Codex
    if (-not $codex) { throw 'Install Codex Desktop for Windows before installing Codex Auto Resume.' }
}

if ($BundledPayload) {
    $version = (Get-Content -LiteralPath (Join-Path $BundledPayload 'runtime-id.txt') -Raw).Trim()
    if ($version -notmatch '^v1\.2-[0-9a-f]{16}$') { throw 'Invalid bundled runtime version.' }
    $target = Join-Path $root $version
    $basePython = Join-Path $target '.venv/Scripts/python.exe'
    $marketplace = Join-Path $target 'marketplace'
    if (-not (Test-Path -LiteralPath (Join-Path $target 'ready') -PathType Leaf)) {
        New-Item -ItemType Directory -Force -Path $root | Out-Null
        $staging = Join-Path $root ('.install-' + [guid]::NewGuid().ToString('N'))
        Copy-Item -LiteralPath (Join-Path $BundledPayload 'runtime') -Destination $staging -Recurse -Force
        foreach ($name in @('mcp.json', '.codex-plugin/plugin.json')) {
            $path = Join-Path $staging "marketplace/plugins/codex-auto-resume/$name"
            $config = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
            $config.mcpServers.'autoresume-panel'.command = $basePython
            if ($name -eq 'mcp.json') { $config.mcpServers.'autoresume-panel'.args = @('-B', '${PLUGIN_ROOT}/launch-panel.py') }
            [IO.File]::WriteAllText($path, ($config | ConvertTo-Json -Depth 15), [Text.UTF8Encoding]::new($false))
        }
        New-Item -ItemType File -Path (Join-Path $staging 'ready') | Out-Null
        # Both paths are children of panel-runtime and the destination is a validated version.
        [IO.Directory]::Move($staging, $target)
    }
} else {
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
    } finally { $sha256.Dispose() }
    $version = 'v1.2-' + ([BitConverter]::ToString($digest)).Replace('-', '').Substring(0, 16).ToLowerInvariant()
    $target = Join-Path $root $version
    $ready = Join-Path $target 'ready'
    if (-not (Test-Path -LiteralPath $ready) -or -not (Test-Path -LiteralPath (Join-Path $target '.venv/Scripts/python.exe'))) {
        Copy-RuntimeSource $target
        & uv sync --project $target --locked --python $basePython
        if ($LASTEXITCODE -ne 0) { throw 'Could not install locked Python dependencies.' }
        New-Item -ItemType File -Force -Path $ready | Out-Null
    }
}

if (-not $RuntimeOnly) { Install-Plugin $codex $marketplace $basePython }
$temp = Join-Path $root ([guid]::NewGuid().ToString('N') + '.json')
[IO.File]::WriteAllText($temp, ((@{ version = $version } | ConvertTo-Json -Compress)), [Text.UTF8Encoding]::new($false))
& $basePython -B -c 'import os,sys; os.replace(sys.argv[1], sys.argv[2])' $temp $pointer
if ($LASTEXITCODE -ne 0) { throw 'Could not activate the installed panel runtime.' }

Write-Output "Codex Auto Resume runtime ready ($version)."

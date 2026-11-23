param([Parameter(Mandatory)][string]$Payload)

$ErrorActionPreference = 'Stop'
$repository = Split-Path -Parent $PSScriptRoot
$oldLocal = $env:LOCALAPPDATA
$oldPath = $env:Path
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ('autoresume-payload-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $sandbox | Out-Null
try {
    $env:LOCALAPPDATA = $sandbox
    $env:Path = Join-Path $env:SystemRoot 'System32'
    $root = Join-Path $sandbox 'CodexAutoResumeGUI/panel-runtime'
    $previous = Join-Path $root 'v1.2-0000000000000000'
    New-Item -ItemType Directory -Force -Path $previous | Out-Null
    [IO.File]::WriteAllText((Join-Path $previous 'ready'), 'existing task runtime')
    [IO.File]::WriteAllText((Join-Path $root 'current.json'), '{"version":"v1.2-0000000000000000"}')
    & (Join-Path $repository 'Install-Panel.ps1') -BundledPayload $Payload -RuntimeOnly
    if ([IO.File]::ReadAllText((Join-Path $previous 'ready')) -ne 'existing task runtime') {
        throw 'The upgrade altered an existing task runtime.'
    }
    $version = (Get-Content -LiteralPath (Join-Path $root 'current.json') -Raw | ConvertFrom-Json).version
    $runtime = Join-Path $root $version
    $python = Join-Path $runtime '.venv/Scripts/python.exe'
    & $python -B (Join-Path $PSScriptRoot 'check-mcp.py') $runtime
    if ($LASTEXITCODE -ne 0) { throw 'The embedded MCP server did not pass its smoke test.' }
    & (Join-Path $repository 'Install-Panel.ps1') -BundledPayload $Payload -RuntimeOnly
    if ($version -eq 'v1.2-0000000000000000' -or
        (Get-Content -LiteralPath (Join-Path $root 'current.json') -Raw | ConvertFrom-Json).version -ne $version) {
        throw 'Reinstall changed the immutable runtime.'
    }
} finally {
    $env:Path = $oldPath
    $env:LOCALAPPDATA = $oldLocal
    $directory = Get-Item -LiteralPath $sandbox
    if ($directory.Parent.FullName -eq [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') -and
        $directory.Name -match '^autoresume-payload-[0-9a-f]{32}$' -and
        -not ($directory.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        [IO.Directory]::Delete($directory.FullName, $true)
    }
}

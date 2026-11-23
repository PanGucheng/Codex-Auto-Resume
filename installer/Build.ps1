param([string]$InnoCompiler = '', [switch]$PayloadOnly)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$plugin = Join-Path $repo 'plugins/codex-auto-resume'
$version = (Get-Content -LiteralPath (Join-Path $plugin 'plugin.json') -Raw | ConvertFrom-Json).version
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'The plugin manifest must contain a numeric x.y.z version.' }
if ($env:GITHUB_REF_TYPE -eq 'tag' -and $env:GITHUB_REF_NAME -ne "v$version") {
    throw "The tag must match the manifest version v$version."
}
if (-not (Test-Path -LiteralPath (Join-Path $plugin 'panel.html') -PathType Leaf)) { throw 'Build the panel first.' }
if (-not $PayloadOnly -and (-not $InnoCompiler -or -not (Test-Path -LiteralPath $InnoCompiler -PathType Leaf))) {
    throw 'Pass the path to ISCC.exe with -InnoCompiler.'
}

$dist = Join-Path $repo 'dist/windows'
$work = Join-Path $dist ('build-' + [guid]::NewGuid().ToString('N'))
$payload = Join-Path $work 'payload'
$runtime = Join-Path $payload 'runtime'
# Preserve the installed path used by the existing panel launcher and scheduled workers.
$pythonRoot = Join-Path $runtime '.venv/Scripts'
$marketplace = Join-Path $runtime 'marketplace'
New-Item -ItemType Directory -Force -Path $pythonRoot, $marketplace | Out-Null

$pythonVersion = '3.14.10'
$pythonSha256 = '90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159'
$archiveName = "python-$pythonVersion-embed-amd64.zip"
$archive = Join-Path $dist $archiveName
if (-not (Test-Path -LiteralPath $archive -PathType Leaf) -or (Get-FileHash -LiteralPath $archive).Hash -ne $pythonSha256) {
    $download = Join-Path $work $archiveName
    curl.exe --fail --location --silent --show-error --connect-timeout 20 --max-time 120 --retry 2 "https://www.python.org/ftp/python/$pythonVersion/$archiveName" --output $download
    if ($LASTEXITCODE -ne 0 -or (Get-FileHash -LiteralPath $download).Hash -ne $pythonSha256) {
        throw 'The embedded Python download failed or did not match its published SHA-256.'
    }
    Copy-Item -LiteralPath $download -Destination $archive -Force
}
Expand-Archive -LiteralPath $archive -DestinationPath $pythonRoot
[IO.File]::WriteAllLines((Join-Path $pythonRoot 'python314._pth'),
    @('python314.zip', '.', '..\..', 'Lib\site-packages', 'import site'), [Text.UTF8Encoding]::new($false))
& (Join-Path $repo 'Install-Panel.ps1') -StageRuntimeDirectory $runtime

$requirements = Join-Path $work 'requirements.txt'
& uv export --project (Join-Path $repo 'panel') --locked --no-dev --no-emit-project --no-annotate --no-header --output-file $requirements | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Could not export locked Python dependencies.' }
$pipArguments = @('pip', 'install', '--no-deps', '--only-binary', ':all:', '--require-hashes',
                  '--python-version', $pythonVersion, '--python-platform', 'x86_64-pc-windows-msvc',
                  '--target', (Join-Path $pythonRoot 'Lib/site-packages'), '-r', $requirements)
& uv @pipArguments
if ($LASTEXITCODE -ne 0) { throw 'Could not bundle locked Python dependencies.' }

Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'stop-installed-plans.py') -Destination $runtime
Copy-Item -LiteralPath (Join-Path $repo 'LICENSE') -Destination (Join-Path $runtime 'PROJECT-LICENSE.txt')
$marketplaceManifest = Join-Path $marketplace '.agents/plugins'
New-Item -ItemType Directory -Force -Path $marketplaceManifest, (Join-Path $marketplace 'plugins') | Out-Null
Copy-Item -LiteralPath (Join-Path $repo '.agents/plugins/marketplace.json') -Destination $marketplaceManifest
Copy-Item -LiteralPath $plugin -Destination (Join-Path $marketplace 'plugins') -Recurse -Force

# Include the interpreter, locked packages, code, and plugin files in the immutable runtime identity.
$inventory = (Get-ChildItem -LiteralPath $runtime -Force -Recurse -File | Sort-Object FullName | ForEach-Object {
    $_.FullName.Substring($runtime.Length + 1) + ' ' + (Get-FileHash -LiteralPath $_.FullName).Hash
}) -join [char]10
$sha256 = [Security.Cryptography.SHA256]::Create()
try {
    $digest = $sha256.ComputeHash([Text.Encoding]::UTF8.GetBytes($inventory))
} finally { $sha256.Dispose() }
$runtimeId = 'v1.2-' + ([BitConverter]::ToString($digest)).Replace('-', '').Substring(0, 16).ToLowerInvariant()
[IO.File]::WriteAllText((Join-Path $payload 'runtime-id.txt'), $runtimeId, [Text.UTF8Encoding]::new($false))

& (Join-Path $PSScriptRoot 'Test-Payload.ps1') -Payload $payload
if ($PayloadOnly) {
    Write-Output "Payload: $payload"
    return
}

$installerName = "Codex-Auto-Resume-$version-Setup-x64"
$compilerArguments = @("/DAppVersion=$version", "/DProjectRoot=$repo", "/DPayload=$payload",
                       "/O$dist", "/F$installerName", (Join-Path $PSScriptRoot 'Setup.iss'))
& $InnoCompiler @compilerArguments
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup could not build the installer.' }
$installer = Join-Path $dist "$installerName.exe"
$checksum = (Get-FileHash -LiteralPath $installer).Hash.ToLowerInvariant()
[IO.File]::WriteAllText("$installer.sha256", "$checksum  $installerName.exe" + [char]10)
Write-Output $installer

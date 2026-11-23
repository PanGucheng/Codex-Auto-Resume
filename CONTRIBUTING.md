# Contributing · 贡献指南

Issues and pull requests are welcome in Chinese or English. For a bug, include the Windows version, Codex Desktop and CLI versions, plugin version, and the steps to reproduce it. See [SECURITY.md](SECURITY.md) for private vulnerability reporting.

## Develop on Windows

Use Python 3.10+ and Node.js 24. The regression tests use temporary databases and mock scheduling and Codex calls.

```powershell
py -3 -X utf8 -m unittest discover -s tests -v
Push-Location panel
npm ci
npm run typecheck
npm run build
Pop-Location
```

Commit the regenerated `plugins/codex-auto-resume/panel.html` with panel source changes. When bundled dependencies change, refresh [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt) from the exact package versions included by esbuild. The build embeds the project license and these notices in the standalone HTML.

For changes to the native entry point or installer, run the optional host check from the repository root with `uv` and `codex` on `PATH`.

```powershell
py -3 -X utf8 tests/check_native_host.py
```

It installs into temporary directories, opens two ephemeral App Server conversations and checks panel discovery, scoped bindings, and the HTML resource. The check starts zero model turns.

To validate the protected shared WebSocket transport on Windows, install the locked Python dependencies and run the isolated shared host check. You can pass the path of the desktop-bundled `codex.exe` to test that particular build.

```powershell
uv sync --project panel --locked
.\panel\.venv\Scripts\python.exe -X utf8 tests/check_shared_host.py
```

## Build the Windows installer

Build the panel first with `npm run build` from `panel/`. With `uv` and the Inno Setup 6 compiler installed, run from the repository root.

```powershell
.\installer\Build.ps1 -InnoCompiler 'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'
```

The build checks the embedded interpreter's SHA-256, installs Windows wheels from the committed lockfile, and starts the MCP server from a clean installation with system Python and uv removed from `PATH`. The installer and checksum are written to `dist/windows/`.

Push to `main` to build the `windows-x64-installer` artifact in GitHub Actions. Push `vX.Y.Z` to publish it to GitHub Releases. Use the same version in the plugin manifest, panel and Python metadata, and generated HTML before pushing a release tag.

## Submit a change

Describe the user-visible change, the tests you ran, and any Windows or Codex versions needed to reproduce it. Include a screenshot with synthetic data for UI changes when it helps explain them. Keep credentials, conversation IDs and content, private paths, and local databases out of public reports.

The [design notes](DESIGN.md) describe the scheduler and MCP integration. The [project license](LICENSE) covers contributions to this repository.

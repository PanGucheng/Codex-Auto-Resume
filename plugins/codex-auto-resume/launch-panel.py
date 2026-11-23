"""Start the installed MCP server with the panel from this plugin version."""

import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    root = Path(os.environ["LOCALAPPDATA"]) / "CodexAutoResumeGUI" / "panel-runtime"
    try:
        record = json.loads((root / "current.json").read_text(encoding="utf-8-sig"))
        runtime = (root / record["version"]).resolve(strict=True)
        if runtime.parent != root.resolve():
            raise ValueError("Invalid runtime directory")
        python = runtime / ".venv" / "Scripts" / "python.exe"
        bundle = Path(__file__).with_name("panel.html")
        return subprocess.call([str(python), str(runtime / "panel_server.py"), str(bundle)], cwd=runtime)
    except (OSError, KeyError, ValueError) as exc:
        print(f"Codex Auto Resume panel: run Install-Panel.ps1 to set up the local runtime ({exc}).", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

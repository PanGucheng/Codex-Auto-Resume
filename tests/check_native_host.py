"""Opt-in check of the local plugin through Codex App Server, without model turns."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_client import AppServer, Connection


def main():
    artifact = Path(__file__).resolve().parents[1]
    codex = Connection.local().resolve_executable()
    with tempfile.TemporaryDirectory(prefix="autoresume-native-check-") as temp:
        home = Path(temp) / "home"
        home.mkdir()
        with patch.dict(os.environ, CODEX_HOME=str(home), LOCALAPPDATA=temp):
            ps = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            subprocess.run([str(ps), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(artifact / "Install-Panel.ps1"), "-RuntimeOnly"], check=True)
            subprocess.run([str(codex), "plugin", "marketplace", "add", str(artifact)], check=True)
            subprocess.run([str(codex), "plugin", "add", "codex-auto-resume@codex-autoresume-local"], check=True)
            with AppServer(Connection(str(home), str(codex)), cwd=temp) as server:
                keys = []
                for _ in range(2):
                    tid = server.request("thread/start", {"cwd": temp, "ephemeral": True})["thread"]["id"]
                    rows = server.request("mcpServerStatus/list", {"threadId": tid}, timeout=35)["data"]
                    row = next((entry for entry in rows if any(tool["name"] == "resume_open"
                               for tool in entry["tools"].values())), None)
                    assert row is not None, f"Panel tool missing; discovered servers: {[(r['name'], list(r['tools'])) for r in rows]}"
                    tool = next(tool for tool in row["tools"].values() if tool["name"] == "resume_open")
                    assert tool["_meta"]["openai/ui"]["entrypoints"] == [{"type": "thread"}]
                    result = server.request("mcpServer/tool/call", {"threadId": tid, "server": row["name"],
                                            "tool": "resume_open", "arguments": {}})
                    keys.append(result["_meta"]["binding"])
                    resource = server.request("mcpServer/resource/read", {"threadId": tid, "server": row["name"],
                                              "uri": "ui://codex-auto-resume/panel.html"})
                    assert any("<title>Codex Auto Resume</title>" in part.get("text", "") for part in resource["contents"])
                    result = server.request("mcpServer/tool/call", {"threadId": tid, "server": row["name"], "tool": "resume_bind",
                                            "arguments": {"key": keys[-1]}})["structuredContent"]
                    assert result["state"] == "unbound"  # An ephemeral probe cannot be selected for scheduled execution.
                    print("Host supplied a thread ID:", not result["reason"].startswith("Codex 未提供"))
                    thread = server.request("thread/read", {"threadId": tid, "includeTurns": False})["thread"]
                    assert thread["status"]["type"] in ("idle", "notLoaded") and not thread["turns"]
                assert keys[0] != keys[1]
                print("Codex discovered the thread entry and resource, opened two independent panels, and started no model turns.")


if __name__ == "__main__":
    main()

"""Test an isolated protected Windows App Server with two concurrent clients."""

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from codex_client import AppServer, Connection, shared_state_file, shared_url
from shared_host import serve_shared, stop


def install_panel(executable, home, temp):
    project = Path(__file__).resolve().parents[1]
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    env = {**os.environ, "CODEX_HOME": str(home), "LOCALAPPDATA": temp}
    commands = ([str(powershell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(project / "Install-Panel.ps1"), "-RuntimeOnly"],
                [str(executable), "plugin", "marketplace", "add", str(project)],
                [str(executable), "plugin", "add", "codex-auto-resume@codex-autoresume-local"])
    for command in commands:
        subprocess.run(command, env=env, check=True, timeout=45)


def verify_panel(owner, thread_id):
    rows = owner.request("mcpServerStatus/list", {"threadId": thread_id}, timeout=35)["data"]
    plugin = next((row for row in rows if any(tool["name"] == "resume_open" for tool in row["tools"].values())), None)
    assert plugin is not None, "The installed panel MCP server was not discovered"
    params = {"threadId": thread_id, "server": plugin["name"]}
    opened = owner.request("mcpServer/tool/call", {**params, "tool": "resume_open", "arguments": {}})
    bound = owner.request("mcpServer/tool/call", {**params, "tool": "resume_bind",
                                                  "arguments": {"key": opened["_meta"]["binding"]}})
    result = bound.get("structuredContent") or {}
    assert result.get("state") == "bound" and result.get("shared") is True and result.get("id") == thread_id, result
    print("The installed native MCP panel used the shared backend for its exact conversation.")


def verify(connection, panel):
    endpoint = shared_url(connection.home)
    for url, origin in ((endpoint.rsplit("/", 1)[0] + "/wrong", None), (endpoint, "http://example.test")):
        try:
            with connect(url, proxy=None, origin=origin, open_timeout=3):
                raise AssertionError("Unauthorized connection was accepted")
        except InvalidStatus as exc:
            assert exc.response.status_code in (403, 404)

    with AppServer(connection) as owner:
        threads = [owner.request("thread/start", {"cwd": connection.home, "ephemeral": False})["thread"]["id"]
                   for _ in range(2)]
        for thread_id in threads:
            owner.request("thread/shellCommand", {"threadId": thread_id, "command": "Write-Output autoresume_probe",
                                                  "timeoutMs": 3000}, watch_turn=True)
        completed = set()
        deadline = time.monotonic() + 12
        while len(completed) < 2:
            event = owner.pending.pop(0) if owner.pending else owner._receive(deadline)
            if event.get("method") == "turn/completed":
                params = event["params"]
                assert params["threadId"] in threads and params["turn"]["status"] == "completed"
                completed.add(params["threadId"])

        def access(item):
            index, thread_id = item
            with AppServer(connection) as worker:
                assert thread_id in worker.request("thread/loaded/list", {})["data"]
                reply = worker.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
                assert reply["thread"]["id"] == thread_id
                worker.request("thread/name/set", {"threadId": thread_id, "name": f"Shared probe {index}"})

        with ThreadPoolExecutor(2) as pool:
            list(pool.map(access, enumerate(threads)))
        for index, thread_id in enumerate(threads):
            assert owner.read_thread(thread_id)["name"] == f"Shared probe {index}"
        if panel:
            verify_panel(owner, threads[0])
    print("Two clients reused both saved threads. Gateway rejected a wrong key and browser-origin access. No model turns.")


async def main(executable, panel):
    with tempfile.TemporaryDirectory(prefix="autoresume-shared-host-check-") as temp:
        home = Path(temp) / "home"
        home.mkdir()
        if panel:
            install_panel(executable, home, temp)
        with patch.dict(os.environ, LOCALAPPDATA=temp):
            connection = Connection(str(home), str(executable), shared=True)
            host = asyncio.create_task(serve_shared(connection))
            try:
                for _ in range(100):
                    if host.done():
                        await host
                    if shared_state_file().exists():
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("No shared endpoint was published.")
                await asyncio.to_thread(verify, connection, panel)
                await asyncio.to_thread(stop, connection)
                await host
                assert not shared_state_file().exists()
            finally:
                if not host.done():
                    host.cancel()
                    with suppress(asyncio.CancelledError):
                        await host


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", nargs="?", default=Connection.local().resolve_executable())
    parser.add_argument("--panel", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.executable, args.panel))

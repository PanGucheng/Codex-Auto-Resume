"""Run one local Codex App Server for Desktop and scheduled workers."""

import argparse
import asyncio
from hashlib import sha256
from hmac import compare_digest
from http import HTTPStatus
import json
import msvcrt
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from codex_client import AppServer, CodexError, Connection, NO_WINDOW, shared_state_file, shared_url


MAX_MESSAGE = 64 * 1024 * 1024


async def forward(source, destination):
    async for message in source:
        await destination.send(message)


async def relay(client, backend_url, token):
    try:
        async with connect(backend_url, additional_headers={"Authorization": f"Bearer {token}"},
                           proxy=None, compression=None, max_size=MAX_MESSAGE, open_timeout=5) as backend:
            tasks = {asyncio.create_task(forward(client, backend)), asyncio.create_task(forward(backend, client))}
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except (ConnectionClosed, OSError, TimeoutError):
        await client.close(1011, "Shared Codex App Server disconnected")


async def serve_shared(connection):
    import win32api
    import win32con
    import win32job

    state = shared_state_file()
    state.parent.mkdir(parents=True, exist_ok=True)
    with state.with_suffix(".lock").open("ab") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return
        state.unlink(missing_ok=True)
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", 0))
            backend_url = f"ws://127.0.0.1:{port_probe.getsockname()[1]}"

        token = secrets.token_urlsafe(32)
        key = "/" + secrets.token_urlsafe(32)
        child_env = {**os.environ, "CODEX_HOME": connection.home, "CODEX_AUTO_RESUME_SHARED": "1"}
        for name in ("CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_TOOL_CALL_ID"):
            child_env.pop(name, None)
        job = win32job.CreateJobObject(None, "")
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits["BasicLimitInformation"]["LimitFlags"] = win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        try:
            backend = subprocess.Popen([
                str(connection.resolve_executable()), "app-server", "--listen", backend_url,
                "--ws-auth", "capability-token", "--ws-token-sha256", sha256(token.encode()).hexdigest(),
            ], env=child_env, cwd=connection.home, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, creationflags=NO_WINDOW)
            try:
                handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, backend.pid)
                try:
                    win32job.AssignProcessToJobObject(job, handle)
                finally:
                    handle.Close()

                for attempt in range(50):
                    if backend.poll() is not None:
                        raise CodexError("共享 Codex App Server 启动失败。")
                    try:
                        async with connect(backend_url, proxy=None, open_timeout=1,
                                           additional_headers={"Authorization": f"Bearer {token}"}):
                            break
                    except (OSError, TimeoutError):
                        await asyncio.sleep(0.2)
                else:
                    raise CodexError("等待共享 Codex App Server 启动超时。")

                stopping = asyncio.Event()

                async def authorize(client, request):
                    if request.headers.get_all("Origin"):
                        return client.respond(HTTPStatus.FORBIDDEN, "Browser connections are unavailable\n")
                    if not request.path.isascii() or not any(compare_digest(request.path, path)
                                                            for path in (key, key + "/stop")):
                        return client.respond(HTTPStatus.NOT_FOUND, "Not found\n")
                    return None

                async def handle(client):
                    if client.request.path == key + "/stop":
                        await client.send("stopping")
                        stopping.set()
                        await client.close()
                        return
                    await relay(client, backend_url, token)

                async with serve(handle, "127.0.0.1", 0, origins=[None], compression=None, max_size=MAX_MESSAGE,
                                 open_timeout=5, process_request=authorize) as gateway:
                    port = gateway.sockets[0].getsockname()[1]
                    temp = state.with_suffix(".tmp")
                    temp.write_text(json.dumps({"home": connection.home, "url": f"ws://127.0.0.1:{port}{key}"}),
                                    encoding="utf-8")
                    temp.replace(state)
                    while backend.poll() is None and not stopping.is_set():
                        await asyncio.sleep(1)
            finally:
                if backend.poll() is None:
                    backend.terminate()
                    backend.wait(timeout=5)
                state.unlink(missing_ok=True)
        finally:
            job.Close()


def current_endpoint(connection):
    try:
        with AppServer(connection):
            return shared_url(connection.home)
    except CodexError:
        return None


def launch(connection, desktop):
    url = current_endpoint(connection)
    if url is None:
        log = shared_state_file().with_name("shared-server.log")
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as output:
            supervisor = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--serve",
                                           "--codex", connection.executable, "--home", connection.home],
                                          stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                          creationflags=NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
        deadline = time.monotonic() + 20
        while url is None and time.monotonic() < deadline:
            if supervisor.poll() not in (None, 0):
                break
            time.sleep(0.3)
            url = current_endpoint(connection)
        if url is None:
            raise CodexError(f"共享服务未就绪。请查看 {log}。")

    env = {**os.environ, "CODEX_APP_SERVER_WS_URL": url, "CODEX_HOME": connection.home}
    env.pop("CODEX_APP_SERVER_FORCE_CLI", None)
    env["NO_PROXY"] = ",".join(filter(None, (env.get("NO_PROXY", ""), "127.0.0.1", "localhost")))
    subprocess.Popen([desktop], cwd=str(Path(desktop).parent), env=env)


def stop(connection):
    from websockets.sync.client import connect as connect_sync

    with AppServer(connection) as server:
        for thread_id in server.request("thread/loaded/list", {})["data"]:
            snapshot = server.request("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
            if snapshot.get("status", {}).get("type") == "active":
                raise CodexError("共享服务中仍有回合正在运行。完成后再停止共享服务。")
    with connect_sync(shared_url(connection.home) + "/stop", proxy=None, open_timeout=5) as peer:
        if peer.recv(timeout=5) != "stopping":
            raise CodexError("共享服务没有确认停止。")
    deadline = time.monotonic() + 5
    while shared_state_file().exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    if shared_state_file().exists():
        raise CodexError("等待共享服务停止超时。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--serve", action="store_true")
    action.add_argument("--stop", action="store_true")
    action.add_argument("--desktop")
    parser.add_argument("--codex", required=True)
    parser.add_argument("--home", default=Connection.local().home)
    args = parser.parse_args()
    connection = Connection(str(Path(args.home).resolve()), args.codex, shared=True)
    if args.serve:
        asyncio.run(serve_shared(connection))
    elif args.stop:
        stop(connection)
    else:
        launch(connection, args.desktop)

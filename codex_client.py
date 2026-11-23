"""Small client for Codex App Server's JSONL stdio transport."""

from functools import lru_cache
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass


VERSION = "1.4.2"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FIVE_HOUR_WINDOW = 300
WEEKLY_WINDOW = 10080
RESET_DELAY_SECONDS = 60
MIN_SCHEDULE_LEAD_SECONDS = 20


class CodexError(RuntimeError):
    pass


class RequestError(CodexError):
    def __init__(self, method, error):
        self.code = error.get("code")
        self.data = error.get("data")
        super().__init__(f"{method}: {error.get('message', '请求失败')}")


@lru_cache(maxsize=1)
def desktop_executable():
    system_root = os.environ.get("SystemRoot")
    if not system_root:
        return None
    powershell = Path(system_root) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-Command",
             "(Get-AppxPackage OpenAI.Codex | Select-Object -First 1).InstallLocation"],
            capture_output=True, text=True, errors="replace", timeout=10, creationflags=NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0 and result.stdout.strip():
        path = Path(result.stdout.strip()) / "app/resources/codex.exe"
        if path.is_file():
            return path
    return None


@dataclass(frozen=True)
class Connection:
    home: str
    executable: str = ""
    shared: bool = False

    @classmethod
    def local(cls, executable=""):
        return cls(str(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()), executable,
                   os.environ.get("CODEX_AUTO_RESUME_SHARED") == "1")

    def resolve_executable(self):
        if self.executable:
            path = Path(self.executable)
            if path.is_file():
                return path
            raise CodexError(f"找不到指定的 Codex：{path}")
        if packaged := desktop_executable():
            return packaged
        in_path = shutil.which("codex.exe")
        if in_path:
            return Path(in_path)
        local = Path(os.environ["LOCALAPPDATA"])
        desktop = sorted(
            (local / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"),
            key=lambda file: file.stat().st_mtime,
            reverse=True,
        )
        npm = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "npm"
        roots = [npm, *(Path(p) for p in os.get_exec_path() if p)]
        npm_binaries = []
        for root in roots:
            package = root / "node_modules" / "@openai" / "codex"
            npm_binaries.extend(package.glob("node_modules/@openai/codex-win32-*/vendor/*/*/codex.exe"))
            npm_binaries.extend(package.glob("node_modules/@openai/codex-win32-*/vendor/*/bin/codex.exe"))
            npm_binaries.extend(package.glob("vendor/*/*/codex.exe"))
        for path in [*desktop, local / "Microsoft" / "WinGet" / "Links" / "codex.exe", *npm_binaries]:
            if path.is_file():
                return path
        raise CodexError("找不到 Codex CLI。请在窗口中选择可用的 codex.exe。")


def error_text(error):
    return " ".join(str(error).split())[:1500] or type(error).__name__


def is_usage_limit_error(error):
    return isinstance(error, dict) and error.get("codexErrorInfo") == "usageLimitExceeded"


def shared_state_file():
    return Path(os.environ["LOCALAPPDATA"]) / "CodexAutoResumeGUI" / "shared-server.json"


def shared_url(home):
    try:
        state = json.loads(shared_state_file().read_text(encoding="utf-8"))
        url = state["url"]
        if (Path(state["home"]).resolve() != Path(home).resolve()
                or not re.fullmatch(r"ws://127\.0\.0\.1:[1-9][0-9]{0,4}/[A-Za-z0-9_-]{43}", url)):
            raise ValueError("invalid shared endpoint")
        return url
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise CodexError("共享 Codex 服务不可用。请使用 Start-SharedCodex.ps1 启动。") from exc


@dataclass(frozen=True)
class QuotaWindow:
    used_percent: int
    resets_at: int | None


@dataclass(frozen=True)
class CodexQuota:
    five_hours: QuotaWindow | None
    weekly: QuotaWindow | None
    limit_reason: str | None = None
    ordinary_usage_allowed: bool | None = None

    @classmethod
    def from_response(cls, response):
        buckets = response.get("rateLimitsByLimitId") or {}
        default = response.get("rateLimits") or {}
        standard = buckets.get("codex")
        if standard is None:
            standard = default if default.get("limitId") in (None, "codex") else {}
        windows = {}
        for key in ("primary", "secondary"):
            source = standard.get(key) or {}
            minutes, used = source.get("windowDurationMins"), source.get("usedPercent")
            if minutes not in (FIVE_HOUR_WINDOW, WEEKLY_WINDOW) or type(used) is not int:
                continue
            reset = source.get("resetsAt")
            windows[minutes] = QuotaWindow(used, reset if type(reset) is int and reset > 0 else None)
        return cls(windows.get(FIVE_HOUR_WINDOW), windows.get(WEEKLY_WINDOW),
                   standard.get("rateLimitReachedType"), response.get("ordinaryUsageAllowed"))

    def resume_time(self, now):
        exhausted = [window for window in (self.five_hours, self.weekly)
                     if window and window.used_percent >= 100]
        if self.limit_reason not in (None, "rate_limit_reached"):
            raise CodexError("当前受工作区额度限制，暂时无法计算续跑时间。")
        if exhausted:
            windows = exhausted
        elif self.ordinary_usage_allowed is False or self.limit_reason == "rate_limit_reached":
            raise CodexError("Codex 报告额度受限，暂未指明耗尽的窗口。")
        else:
            windows = [self.five_hours] if self.five_hours else []
        if not windows or any(window.resets_at is None for window in windows):
            raise CodexError("Codex 暂未提供所需的额度恢复时间。")
        reset = max(window.resets_at for window in windows)
        if reset <= now:
            raise CodexError("额度恢复时间已过，请刷新额度。")
        return reset + RESET_DELAY_SECONDS


class AppServer:
    def __init__(self, connection, cwd=None, on_interaction=None, *, experimental=False):
        self.connection = connection
        self.cwd = cwd
        self.on_interaction = on_interaction or (lambda method: None)
        self.experimental = experimental
        self.process = None
        self.websocket = None
        self.messages = queue.Queue()
        self.pending = []
        self.request_id = 0
        self.submitted_thread = None
        self.submitted_turn = None

    def __enter__(self):
        if not Path(self.connection.home).is_dir():
            raise CodexError("CODEX_HOME 不存在。请先运行 Codex 并确认登录设置。")
        try:
            if self.connection.shared:
                self._open_shared()
            else:
                self._open_stdio()
            params = {"clientInfo": {
                "name": "codex-autoresume-gui", "title": "Codex Auto Resume", "version": VERSION,
            }}
            if self.experimental:
                params["capabilities"] = {"experimentalApi": True}
            initialized = self.request("initialize", params)
            if self.connection.shared and Path(initialized.get("codexHome") or ".").resolve() != Path(self.connection.home).resolve():
                raise CodexError("共享 Codex 服务使用了另一份会话目录。")
            self._write({"method": "initialized"})
        except BaseException:
            self.close()
            raise
        return self

    def _open_shared(self):
        url = shared_url(self.connection.home)
        try:
            from websockets.sync.client import connect
            self.websocket = connect(url, proxy=None, compression=None, open_timeout=5, close_timeout=2,
                                     max_size=64 * 1024 * 1024)
        except Exception as exc:
            raise CodexError("连接共享 Codex 服务失败。请使用 Start-SharedCodex.ps1 启动。") from exc
        threading.Thread(target=self._read_packets, args=(self.websocket,), daemon=True).start()

    def _open_stdio(self):
        environment = os.environ.copy()
        environment["CODEX_HOME"] = self.connection.home
        for name in ("CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_TOOL_CALL_ID"):
            environment.pop(name, None)
        self.process = subprocess.Popen(
            [str(self.connection.resolve_executable()), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=self.cwd, env=environment, creationflags=NO_WINDOW,
        )
        threading.Thread(target=self._read_stdio_packets, args=(self.process.stdout,), daemon=True).start()

    def __exit__(self, *exc):
        self.close()

    def close(self):
        if self.websocket is not None:
            self.websocket.close()
            return
        if self.process is None:
            return
        try:
            self.process.stdin.close()
        except OSError:
            pass
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        # The reader owns stdout. A Windows child of Codex can retain the pipe
        # after Codex exits, so closing the stream here can block the worker.

    def _read_stdio_packets(self, incoming):
        with incoming:
            self._read_packets(incoming)

    def _read_packets(self, incoming):
        try:
            for line in incoming:
                try:
                    packet = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(packet, dict):
                    self.messages.put(packet)
        except Exception:
            pass
        finally:
            self.messages.put(None)

    def _write(self, packet):
        message = json.dumps(packet, ensure_ascii=False)
        try:
            if self.websocket is not None:
                self.websocket.send(message)
            else:
                self.process.stdin.write((message + "\n").encode("utf-8"))
                self.process.stdin.flush()
        except Exception as exc:
            raise CodexError("Codex App Server 已断开。") from exc

    def _receive(self, deadline):
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexError("等待 Codex App Server 响应超时。")
            try:
                packet = self.messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise CodexError("等待 Codex App Server 响应超时。") from exc
            if packet is None:
                if self.connection.shared:
                    raise CodexError("共享 Codex 服务已断开。")
                raise CodexError(f"Codex App Server 已退出（代码 {self.process.poll()}）。")
            if "method" not in packet or "id" not in packet:
                return packet
            params = packet.get("params") or {}
            owns_request = (self.submitted_thread and params.get("threadId") == self.submitted_thread
                            and (not self.submitted_turn or params.get("turnId") in (None, self.submitted_turn)))
            if not self.connection.shared or owns_request:
                self._reject_interaction(packet)

    def _reject_interaction(self, packet):
        method = packet["method"]
        self.on_interaction(method)
        response = {"id": packet["id"]}
        if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
            response["result"] = {"decision": "decline"}
        elif method in ("execCommandApproval", "applyPatchApproval"):
            response["result"] = {"decision": "abort"}
        elif method == "item/permissions/requestApproval":
            response["result"] = {"permissions": {}, "scope": "turn"}
        else:
            response["error"] = {"code": -32000, "message": "Unattended client cannot provide interactive input."}
        self._write(response)

    def request(self, method, params, timeout=20, watch_turn=False):
        self.request_id += 1
        self._write({"id": self.request_id, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            packet = self._receive(deadline)
            if packet.get("id") == self.request_id:
                if "error" in packet:
                    raise RequestError(method, packet["error"])
                if not isinstance(packet.get("result"), dict):
                    raise CodexError(f"{method}: Codex 返回了无法识别的响应。")
                return packet["result"]
            if watch_turn and packet.get("method") in ("turn/completed", "error"):
                self.pending.append(packet)

    def list_threads(self, cursor=None, search=""):
        return self.request("thread/list", {
            "limit": 30, "sortKey": "updated_at", "archived": False, "modelProviders": [],
            "currentAccountOnly": True, "cursor": cursor, "searchTerm": search or None,
        })

    def read_thread(self, thread_id):
        thread = self.request("thread/read", {"threadId": thread_id, "includeTurns": False})["thread"]
        if thread.get("id") != thread_id or thread.get("ephemeral"):
            raise CodexError("只能为可持久恢复的原会话创建任务。")
        if thread.get("canAcceptDirectInput") is False:
            raise CodexError("此会话无法直接接收用户消息。")
        return thread

    def check_account(self):
        result = self.request("account/read", {"refreshToken": False})
        if result.get("requiresOpenaiAuth") and not result.get("account"):
            raise CodexError("当前 Codex 未登录。请在终端运行 codex login 后再验证。")

    def read_quota(self):
        return CodexQuota.from_response(self.request("account/rateLimits/read", {}))

    def models(self):
        cursor, models = None, []
        while True:
            response = self.request("model/list", {"cursor": cursor, "includeHidden": False})
            models.extend(response["data"])
            cursor = response.get("nextCursor")
            if not cursor:
                return models

    def read_goal(self, thread_id):
        try:
            return self.request("thread/goal/get", {"threadId": thread_id}).get("goal")
        except RequestError as exc:
            if exc.code == -32601:
                return None
            raise

    def ensure_idle(self, thread_id):
        result = self.request("thread/turns/list", {"threadId": thread_id, "limit": 1, "itemsView": "notLoaded"})
        turns = result.get("data", [])
        if turns and turns[0].get("status") == "inProgress":
            raise CodexError("目标会话的最近一轮仍在运行。请在该会话空闲时重新创建计划。")

    def add_queued_message(self, thread_id, message, message_id):
        response = self.request("thread/queue/add", {
            "threadId": thread_id, "clientUserMessageId": message_id,
            "input": [{"type": "text", "text": message, "text_elements": []}],
        }, timeout=30)
        submission = response.get("queuedSubmission") or {}
        if submission.get("clientUserMessageId") != message_id or not submission.get("id"):
            raise CodexError("未能核对 Codex 的入队回执。请检查原会话队列。")
        return submission["id"]

    def has_queued_message(self, thread_id, message_id):
        cursor = None
        while True:
            page = self.request("thread/queue/list", {"threadId": thread_id, "cursor": cursor, "limit": 50})
            if any(row.get("clientUserMessageId") == message_id for row in page["data"]):
                return True
            cursor = page.get("nextCursor")
            if not cursor:
                return False

    def find_queued_turn(self, thread_id, message_id, since, turn_id=None):
        cursor = None
        while True:
            page = self.request("thread/turns/list", {
                "threadId": thread_id, "limit": 8, "cursor": cursor,
                "itemsView": "notLoaded" if turn_id else "full",
            })
            for turn in page["data"]:
                if turn_id and turn.get("id") == turn_id:
                    return turn
                if not turn_id and any(item.get("type") == "userMessage" and item.get("clientId") == message_id
                                       for item in turn.get("items", [])):
                    return turn
                started_at = turn.get("startedAt")
                if isinstance(started_at, (int, float)) and started_at < since:
                    return None
            cursor = page.get("nextCursor")
            if not cursor:
                return None

    def start_turn(self, thread_id, message, *, model=None, effort=None):
        self.submitted_thread = thread_id
        self.submitted_turn = None
        params = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": message, "text_elements": []}],
        }
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        result = self.request("turn/start", params, timeout=60, watch_turn=True)
        turn = result.get("turn") or {}
        if not turn.get("id"):
            raise CodexError("Codex 没有返回 turn ID。")
        self.submitted_turn = turn["id"]
        return turn

    def wait_turn(self, thread_id, turn_id, timeout):
        deadline = time.monotonic() + timeout
        turn_error = None
        while True:
            packet = self.pending.pop(0) if self.pending else self._receive(deadline)
            params, method = packet.get("params") or {}, packet.get("method")
            if params.get("threadId") != thread_id:
                continue
            if method == "turn/completed" and params["turn"].get("id") == turn_id:
                turn = params["turn"]
                if turn_error and (is_usage_limit_error(turn_error)
                                   or not (turn.get("error") or {}).get("codexErrorInfo")):
                    turn = {**turn, "error": turn_error}
                return turn
            if params.get("turnId") != turn_id:
                continue
            if method == "error" and not params.get("willRetry") and not is_usage_limit_error(turn_error):
                turn_error = params.get("error")


def read_local_target(connection, thread_id):
    with AppServer(connection) as server:
        thread = server.read_thread(thread_id)
    cwd = thread.get("cwd")
    if not cwd or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise CodexError("目标会话的本机工作目录不可用。")
    return thread


def check_target(connection, thread_id):
    thread = read_local_target(connection, thread_id)
    with AppServer(connection, cwd=thread["cwd"]) as server:
        server.check_account()
    return thread

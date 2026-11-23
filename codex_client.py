"""Small client for Codex App Server's JSONL stdio transport."""

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass


VERSION = "1.1.0"
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


@dataclass(frozen=True)
class Connection:
    home: str
    executable: str = ""

    @classmethod
    def local(cls, executable=""):
        return cls(str(Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()), executable)

    def resolve_executable(self):
        if self.executable:
            path = Path(self.executable)
            if path.is_file():
                return path
            raise CodexError(f"找不到指定的 Codex：{path}")
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
    def __init__(self, connection, cwd=None, on_interaction=None):
        self.connection = connection
        self.cwd = cwd
        self.on_interaction = on_interaction or (lambda method: None)
        self.process = None
        self.messages = queue.Queue()
        self.pending = []
        self.request_id = 0
        self.last_agent_message = ""

    def __enter__(self):
        if not Path(self.connection.home).is_dir():
            raise CodexError("CODEX_HOME 不存在。请先运行 Codex 并确认登录设置。")
        environment = os.environ.copy()
        environment["CODEX_HOME"] = self.connection.home
        for name in ("CODEX_SESSION_ID", "CODEX_THREAD_ID", "CODEX_TOOL_CALL_ID"):
            environment.pop(name, None)
        self.process = subprocess.Popen(
            [str(self.connection.resolve_executable()), "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=self.cwd, env=environment, creationflags=NO_WINDOW,
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        try:
            self.request("initialize", {"clientInfo": {
                "name": "codex-autoresume-gui", "title": "Codex Auto Resume", "version": VERSION,
            }})
            self._write({"method": "initialized"})
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
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
        self.process.stdout.close()

    def _read_stdout(self):
        try:
            for line in self.process.stdout:
                try:
                    packet = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if isinstance(packet, dict):
                    self.messages.put(packet)
        except (OSError, ValueError):
            pass
        finally:
            self.messages.put(None)

    def _write(self, packet):
        try:
            self.process.stdin.write((json.dumps(packet, ensure_ascii=False) + "\n").encode("utf-8"))
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
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
                raise CodexError(f"Codex App Server 已退出（代码 {self.process.poll()}）。")
            if "method" not in packet or "id" not in packet:
                return packet
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
            if watch_turn and packet.get("method") in ("turn/completed", "error", "item/completed"):
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

    def start_turn(self, thread_id, message):
        result = self.request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": message, "text_elements": []}],
        }, timeout=60, watch_turn=True)
        turn = result.get("turn") or {}
        if not turn.get("id"):
            raise CodexError("Codex 没有返回 turn ID。")
        return turn

    def wait_turn(self, thread_id, turn_id, timeout):
        deadline = time.monotonic() + timeout
        self.last_agent_message = ""
        turn_error = None
        while True:
            packet = self.pending.pop(0) if self.pending else self._receive(deadline)
            params, method = packet.get("params") or {}, packet.get("method")
            if params.get("threadId") != thread_id:
                continue
            if method == "turn/completed" and params["turn"].get("id") == turn_id:
                turn = params["turn"]
                if turn_error and not (turn.get("error") or {}).get("codexErrorInfo"):
                    turn = {**turn, "error": turn_error}
                return turn
            if params.get("turnId") != turn_id:
                continue
            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("phase") in (None, "final_answer"):
                    self.last_agent_message = item.get("text") or ""
            elif method == "error" and not params.get("willRetry"):
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

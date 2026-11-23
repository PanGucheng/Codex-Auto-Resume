"""Scope native panel actions to a verified local Codex conversation."""

from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime
from hashlib import sha256
import json
import os
from threading import RLock
import time
import uuid

from codex_client import AppServer, CodexError, Connection, check_target, error_text
from jobs import DEFAULT_MESSAGE, RUNNING_STATES, JobStore, thread_title, validate_thread_id
from local_datetime import parse_local_time


def native_thread_id(meta):
    values = [meta[key] for key in ("threadId", "thread_id") if key in meta]
    if not values or any(not isinstance(value, str) for value in values):
        return None
    try:
        ids = {validate_thread_id(value) for value in values}
    except ValueError:
        return None
    return next(iter(ids)) if len(ids) == 1 else None


def display_time(timestamp):
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S") if timestamp is not None else None


@dataclass
class Binding:
    candidate: str | None
    thread: dict | None = None


class PanelService:
    def __init__(self, store=None):
        self.store = store or JobStore()
        self.bindings = OrderedDict()
        self.bindings_lock = RLock()
        self.connection = Connection.local(self.store.setting("codex_path"))

    def open(self, meta):
        key = uuid.uuid4().hex
        with self.bindings_lock:
            self.bindings[key] = Binding(native_thread_id(meta))
            if len(self.bindings) > 256:
                self.bindings.popitem(last=False)
        return {"binding": key, "defaultMessage": DEFAULT_MESSAGE}

    def binding(self, key):
        with self.bindings_lock:
            if key not in self.bindings:
                raise ValueError("面板连接已失效。请关闭当前面板后重新打开。")
            self.bindings.move_to_end(key)
            return self.bindings[key]

    def bind(self, key, link=None):
        binding = self.binding(key)
        if binding.thread is not None:
            if link is not None and validate_thread_id(link) != binding.thread["id"]:
                raise ValueError("面板已经绑定会话。请在目标对话里打开面板。")
            thread = binding.thread
        else:
            target = validate_thread_id(link) if link is not None else binding.candidate
            if target is None:
                return {"state": "unbound", "reason": "Codex 未提供可验证的当前会话，请粘贴本机会话链接。"}
            try:
                thread = check_target(self.connection, target)
            except (CodexError, OSError) as exc:
                return {"state": "unbound", "reason": error_text(exc)}
            with self.bindings_lock:
                if binding.thread and binding.thread["id"] != thread["id"]:
                    raise ValueError("面板已经绑定会话。请在目标对话里打开面板。")
                binding.thread = thread
        return {"state": "bound", "title": thread_title(thread), "id": thread["id"]}

    def target(self, key):
        thread = self.binding(key).thread
        if thread is None:
            raise ValueError("请先绑定本机会话。")
        return thread

    def quota(self, key):
        binding = self.binding(key)
        cwd = binding.thread["cwd"] if binding.thread else None
        with AppServer(self.connection, cwd=cwd) as server:
            result = server.read_quota()
        suggestion, reason = None, None
        try:
            timestamp = result.resume_time(time.time())
            suggestion = {"epoch": timestamp, "local": display_time(timestamp)}
        except (CodexError, OSError, OverflowError, ValueError) as exc:
            reason = error_text(exc)
        windows = {}
        for name in ("five_hours", "weekly"):
            window = getattr(result, name)
            windows[name] = {**asdict(window), "resetLocal": display_time(window.resets_at)} if window else None
        return {**windows, "suggestion": suggestion, "reason": reason}

    @staticmethod
    def job_summary(row):
        state = row["state"]
        action = None
        if state == "scheduled":
            action = "cancel"
        elif state in RUNNING_STATES and row["continue_enabled"]:
            action = "stop"
        return {"id": row["id"], "state": state, "scheduledLocal": display_time(row["scheduled_at"]),
                "continuous": bool(row["continuous"]), "action": action}

    def jobs(self, key):
        thread = self.target(key)
        self.store.reconcile_workers()
        return {"jobs": [self.job_summary(row) for row in self.store.for_thread(self.connection.home, thread["id"])]}

    def owned_job(self, key, job_id):
        thread = self.target(key)
        row = self.store.get(job_id)
        if (not row or row["thread_id"] != thread["id"]
                or os.path.normcase(row["codex_home"]) != os.path.normcase(self.connection.home)):
            raise ValueError("当前会话没有这个计划。")
        return row

    def events(self, key, job_id):
        row = self.owned_job(key, job_id)
        return {"message": row["message"], "events": [
            {"at": display_time(event["at"]), "detail": event["detail"]}
            for event in self.store.events(job_id)
        ]}

    def create(self, key, request_id, mode, when, message, wake, continuous):
        thread = self.target(key)
        request_id = uuid.UUID(request_id).hex
        identity = [os.path.normcase(self.connection.home), thread["id"], mode, when, message, wake, continuous]
        request = (request_id, sha256(json.dumps(identity).encode()).hexdigest())
        if existing := self.store.submitted_job(request):
            return {"state": "created", "job": self.job_summary(self.owned_job(key, existing))}
        if mode == "quota":
            quota = self.quota(key)
            suggestion = quota["suggestion"]
            if not suggestion or suggestion["epoch"] != when:
                return {"state": "quota_changed", "quota": quota}
            scheduled_at = when
        elif mode == "manual" and isinstance(when, str):
            scheduled_at = parse_local_time(when.replace("T", " "))
        else:
            raise ValueError("请选择发送时间。")
        validated = check_target(self.connection, thread["id"])
        job = self.store.schedule(self.connection, validated, message, scheduled_at, wake, continuous, request)
        return {"state": "created", "job": self.job_summary(self.owned_job(key, job))}

    def stop(self, key, job_id, action):
        row = self.owned_job(key, job_id)
        if action == "cancel" and row["state"] == "cancelled":
            return self.job_summary(row)
        if action == "stop" and row["continuous"] and not row["continue_enabled"]:
            return self.job_summary(row)
        if self.job_summary(row)["action"] != action:
            raise ValueError("计划状态已变化，请刷新后再试。")
        if action == "cancel":
            self.store.cancel(job_id)
        elif action == "stop":
            self.store.stop_followups(job_id)
        return self.job_summary(self.owned_job(key, job_id))

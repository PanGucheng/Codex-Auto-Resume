"""Job storage, Windows scheduler, and the scheduled worker."""

from contextlib import closing, contextmanager
from datetime import datetime
import json
import msvcrt
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from codex_client import (AppServer, CodexError, Connection, MIN_SCHEDULE_LEAD_SECONDS, NO_WINDOW,
                          RequestError, error_text, is_usage_limit_error, read_local_target)


APP_DIR = Path(__file__).resolve().parent
DEFAULT_MESSAGE = "继续完成之前的任务，不要停，直到当前计划完成。"
RUN_LIMIT_SECONDS = 12 * 60 * 60
RUNNING_STATES = ("preparing", "submitting", "accepted")
TASK_PREFIX = "CodexAutoResumeGUI_"
ACTIVE_GOAL_STATES = ("active", "usageLimited")
JOB_COLUMNS = "jobs.*, c.job_id IS NOT NULL AS continuous, c.enabled AS continue_enabled, c.goal_key, c.parent_id"


def default_data_dir():
    return Path(os.environ["LOCALAPPDATA"]) / "CodexAutoResumeGUI"


def task_name(job_id):
    return TASK_PREFIX + job_id


def validate_thread_id(value):
    value = value.strip()
    if value.lower().startswith("codex://threads/"):
        value = value[len("codex://threads/"):].rstrip("/")
    if not value or len(value) > 200 or any(not (ch.isascii() and (ch.isalnum() or ch in "_-.")) for ch in value):
        raise ValueError("请输入有效的 Codex thread ID 或会话链接。")
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return value


def scheduler(op, **params):
    powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run(
            [str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
             str(APP_DIR / "task-scheduler.ps1")],
            input=json.dumps({"op": op, **params}), encoding="utf-8", errors="replace",
            capture_output=True, timeout=25, creationflags=NO_WINDOW, cwd=APP_DIR,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"无法连接 Windows 任务计划程序。{error_text(exc)}") from exc
    if result.returncode:
        raise RuntimeError(f"Windows 任务计划程序操作失败。{error_text(result.stderr or result.stdout)}")
    if op == "list":
        return set(json.loads(result.stdout))


class JobStore:
    def __init__(self, data_dir=None):
        self.root = Path(data_dir or default_data_dir()).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.file = self.root / "jobs.sqlite3"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    message TEXT NOT NULL,
                    scheduled_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    codex_home TEXT NOT NULL,
                    codex_path TEXT NOT NULL,
                    state TEXT NOT NULL,
                    turn_id TEXT,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs (id),
                    at REAL NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS continuations (
                    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
                    parent_id TEXT UNIQUE REFERENCES continuations(job_id),
                    wake INTEGER NOT NULL,
                    goal_key TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS panel_requests (
                    id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    job_id TEXT NOT NULL REFERENCES jobs(id)
                );
                CREATE INDEX IF NOT EXISTS job_events ON events(job_id, seq);
                CREATE UNIQUE INDEX IF NOT EXISTS active_thread_job ON jobs(codex_home, thread_id)
                    WHERE state IN ('preparing', 'submitting', 'accepted');
            """)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.file, timeout=35)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                yield db

    def setting(self, name):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE name = ?", (name,)).fetchone()
        return row[0] if row else ""

    def set_setting(self, name, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (name, value))

    def get(self, job_id):
        with self.db() as db:
            return db.execute(f"SELECT {JOB_COLUMNS} FROM jobs LEFT JOIN continuations c ON c.job_id = jobs.id WHERE jobs.id = ?",
                              (job_id,)).fetchone()

    def all(self):
        with self.db() as db:
            return db.execute(f"SELECT {JOB_COLUMNS} FROM jobs LEFT JOIN continuations c ON c.job_id = jobs.id ORDER BY created_at DESC").fetchall()

    def for_thread(self, home, thread_id):
        with self.db() as db:
            return db.execute(f"""
                SELECT {JOB_COLUMNS} FROM jobs LEFT JOIN continuations c ON c.job_id = jobs.id
                WHERE codex_home = ? COLLATE NOCASE AND thread_id = ?
                ORDER BY (state IN ('scheduled', 'preparing', 'submitting', 'accepted')) DESC, created_at DESC LIMIT 30
            """, (home, thread_id)).fetchall()

    def events(self, job_id):
        with self.db() as db:
            return db.execute("SELECT at, detail FROM events WHERE job_id = ? ORDER BY seq", (job_id,)).fetchall()

    @staticmethod
    def _event(db, job_id, detail):
        db.execute("INSERT INTO events (job_id, at, detail) VALUES (?, ?, ?)", (job_id, time.time(), detail))

    def log(self, job_id, detail):
        with self.db() as db:
            db.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (time.time(), job_id))
            self._event(db, job_id, detail)

    @classmethod
    def _transition(cls, db, job_id, previous, new, detail, turn_id=None):
        result = db.execute("""
            UPDATE jobs SET state = ?, updated_at = ?, turn_id = COALESCE(?, turn_id)
            WHERE id = ? AND state = ?
        """, (new, time.time(), turn_id, job_id, previous))
        if result.rowcount:
            cls._event(db, job_id, detail)
        return bool(result.rowcount)

    def transition(self, job_id, previous, new, detail, turn_id=None):
        with self.db() as db:
            return self._transition(db, job_id, previous, new, detail, turn_id)

    @staticmethod
    def _submitted_job(db, request):
        request_id, fingerprint = request
        row = db.execute("SELECT fingerprint, job_id FROM panel_requests WHERE id = ?", (request_id,)).fetchone()
        if row and row["fingerprint"] != fingerprint:
            raise ValueError("提交编号已用于另一份计划，请重新确认。")
        return row["job_id"] if row else None

    def submitted_job(self, request):
        with self.db() as db:
            return self._submitted_job(db, request)

    def schedule(self, connection, thread, message, scheduled_at, wake, continuous=False, request=None):
        with self.db() as db:
            if continuous or request:
                db.execute("BEGIN IMMEDIATE")
            if request and (existing_job := self._submitted_job(db, request)):
                return existing_job
            if continuous:
                existing = db.execute("""
                    SELECT 1 FROM jobs JOIN continuations c ON c.job_id = jobs.id
                    WHERE codex_home = ? AND thread_id = ? AND c.enabled = 1
                    AND state IN ('scheduled', 'preparing', 'submitting', 'accepted')
                """, (connection.home, validate_thread_id(thread["id"]))).fetchone()
                if existing:
                    raise ValueError("这个会话已有持续续跑计划。可在任务记录中查看或停止。")
            job_id = self._register(db, connection, thread, message, scheduled_at, wake, continuous)
            if request:
                db.execute("INSERT INTO panel_requests VALUES (?, ?, ?)", (*request, job_id))
            return job_id

    def _register(self, db, connection, thread, message, scheduled_at, wake, continuous, parent_id=None, goal_key=None):
        if scheduled_at - time.time() < MIN_SCHEDULE_LEAD_SECONDS:
            raise ValueError("发送时间至少晚于现在 20 秒。")
        if not message.strip():
            raise ValueError("请输入发送内容。")
        if len(message) > 16000:
            raise ValueError("发送内容最多 16,000 个字符。")
        job_id = uuid.uuid4().hex
        now = time.time()
        thread_id = validate_thread_id(thread["id"])
        pythonw = Path(sys.base_prefix) / "pythonw.exe"
        if not pythonw.is_file():
            pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.is_file():
            raise RuntimeError("找不到 pythonw.exe。请用 Windows Python 3.10 或更新版本启动。")
        db.execute("""
            INSERT INTO jobs (id, thread_id, title, cwd, message, scheduled_at, created_at, codex_home, codex_path, state, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'registering', ?)
        """, (job_id, thread_id, thread_title(thread), thread["cwd"], message,
              scheduled_at, now, connection.home, connection.executable, now))
        if continuous:
            db.execute("INSERT INTO continuations (job_id, parent_id, wake, goal_key) VALUES (?, ?, ?, ?)",
                       (job_id, parent_id, wake, goal_key))
        self._event(db, job_id, "开始注册 Windows 一次性计划任务。")
        scheduler(
            "register", name=task_name(job_id), wake=wake,
            when=datetime.fromtimestamp(scheduled_at).astimezone().isoformat(timespec="seconds"),
            executable=str(pythonw), cwd=str(APP_DIR),
            arguments=subprocess.list2cmdline([str(APP_DIR / "main.pyw"), "--run", job_id,
                                              "--data-dir", str(self.root)]),
        )
        self._transition(db, job_id, "registering", "scheduled", "已注册。到点由 Windows 启动。")
        return job_id

    def schedule_followup(self, job, previous, scheduled_at, goal_key, reason):
        job_id = job["id"]
        connection = Connection(job["codex_home"], job["codex_path"])
        thread = {"id": job["thread_id"], "name": job["title"], "cwd": job["cwd"]}
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            options = db.execute("SELECT enabled, wake FROM continuations WHERE job_id = ?", (job_id,)).fetchone()
            if not options["enabled"]:
                self._transition(db, job_id, previous, "stopped", "已按用户要求停止后续续跑。")
                return
            next_time = datetime.fromtimestamp(scheduled_at).strftime("%Y-%m-%d %H:%M:%S")
            if not self._transition(db, job_id, previous, "rescheduled", f"{reason} 下一轮计划 {next_time}。"):
                return
            self._register(db, connection, thread, job["message"], scheduled_at, bool(options["wake"]),
                           True, parent_id=job_id, goal_key=goal_key)

    def stop_followups(self, job_id):
        with self.db() as db:
            result = db.execute("""
                UPDATE continuations SET enabled = 0 WHERE job_id = ? AND enabled = 1 AND EXISTS (
                    SELECT 1 FROM jobs WHERE id = ? AND state IN ('preparing', 'submitting', 'accepted')
                )
            """, (job_id, job_id))
            if not result.rowcount:
                raise RuntimeError("本轮已结束或后续续跑已停止。可在任务记录取消下一条等待中的计划。")
            db.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (time.time(), job_id))
            self._event(db, job_id, "已停止安排后续轮次。当前回合按原流程结束。")

    def cancel(self, job_id):
        if not self.transition(job_id, "scheduled", "cancelled", "已取消，后台进程不会发送。"):
            raise RuntimeError("任务已经开始或结束，无法取消。")
        try:
            scheduler("remove", name=task_name(job_id))
        except RuntimeError as exc:
            self.log(job_id, f"注销 Windows 任务失败。{error_text(exc)}")
            raise RuntimeError(f"本地任务已取消，Windows 条目仍需清理。{error_text(exc)}") from exc
        self.log(job_id, "已注销 Windows 任务。")

    @contextmanager
    def worker_lock(self, job_id):
        lock_dir = self.root / "locks"
        lock_dir.mkdir(exist_ok=True)
        with (lock_dir / f"{job_id}.lock").open("ab") as lock_file:
            lock_file.seek(0)
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)

    def reconcile_workers(self):
        with self.db() as db:
            active_ids = db.execute("SELECT id FROM jobs WHERE state IN ('preparing','submitting','accepted')").fetchall()
        for active in active_ids:
            job_id = active["id"]
            with self.worker_lock(job_id) as acquired:
                if not acquired:
                    continue
                row = self.get(job_id)
                if row["state"] in RUNNING_STATES:
                    new = "failed" if row["state"] == "preparing" else "unknown"
                    self.transition(job_id, row["state"], new, "后台进程已退出。请查看目标会话与任务日志。")


def thread_title(thread):
    return " ".join((thread.get("name") or thread.get("preview") or "未命名会话").split())[:180]


def run_job(store, job_id):
    if len(job_id) != 32 or any(ch not in "0123456789abcdef" for ch in job_id):
        return 2
    store.reconcile_workers()
    with store.worker_lock(job_id) as acquired:
        if not acquired:
            return 0
        job = store.get(job_id)
        if job is None or job["state"] != "scheduled":
            return 0
        if job["scheduled_at"] - time.time() > 2:
            store.log(job_id, "忽略早于计划时间的触发。")
            return 0
        try:
            claimed = store.transition(job_id, "scheduled", "preparing", "已触发，正在检查和恢复原会话。")
        except sqlite3.IntegrityError:
            store.transition(job_id, "scheduled", "failed", "该会话已有本工具启动的回合。")
            return 2
        if not claimed:
            return 0
        return send_job(store, job)


def goal_key(goal):
    return json.dumps([goal.get("createdAt"), goal.get("objective")], ensure_ascii=False) if goal else ""


def goal_stop(goal, expected_key):
    if expected_key is not None and goal_key(goal) != expected_key:
        return "stopped", "会话目标已更换或清除，持续续跑已停止。"
    if goal:
        status = goal.get("status")
        if status == "complete":
            return "completed", "Codex 会话目标已完成，持续续跑结束。"
        if status not in ACTIVE_GOAL_STATES:
            details = {"paused": "会话目标已暂停", "blocked": "会话目标受阻", "budgetLimited": "会话目标本身的 token 预算已耗尽"}
            return "review", f"{details.get(status, '会话目标状态需检查')}，持续续跑已停止。"
    return None


def reschedule_after_quota(store, job, server, previous, key):
    job_id = job["id"]
    try:
        if not store.get(job_id)["continue_enabled"]:
            store.transition(job_id, previous, "stopped", "已按用户要求停止后续续跑。")
            return 0
        goal = server.read_goal(job["thread_id"])
        stopped = goal_stop(goal, key)
        if stopped:
            store.transition(job_id, previous, *stopped)
            return 0
        next_run = server.read_quota().resume_time(time.time())
        store.schedule_followup(job, previous, next_run, key, "本轮遇到 Codex 额度限制。")
        return 0
    except Exception as exc:
        store.transition(job_id, previous, "review", f"后续轮次未能安排。{error_text(exc)}")
        return 2


def send_job(store, job):
    job_id, thread_id = job["id"], job["thread_id"]
    connection = Connection(job["codex_home"], job["codex_path"])
    refused = []
    state = "preparing"

    def log_interaction(method):
        refused.append(method)
        store.log(job_id, f"后台任务未提供交互批准或输入（{method}）。")

    try:
        thread = read_local_target(connection, thread_id)
        with AppServer(connection, cwd=thread["cwd"], on_interaction=log_interaction) as server:
            server.check_account()
            server.ensure_idle(thread_id)
            result = server.request("thread/resume", {"threadId": thread_id, "excludeTurns": True}, timeout=60)
            resumed = result["thread"]
            if resumed.get("id") != thread_id or resumed.get("status", {}).get("type") == "active":
                raise CodexError("原会话未成功恢复，或当前有回合正在运行。")
            key = None
            if job["continuous"]:
                goal = server.read_goal(thread_id)
                stopped = goal_stop(goal, job["goal_key"])
                if stopped:
                    store.transition(job_id, state, *stopped)
                    return 0
                key = goal_key(goal)
            store.transition(job_id, state, "submitting", "已恢复原会话，正在提交一次消息。")
            state = "submitting"
            try:
                turn_id = server.start_turn(thread_id, job["message"])["id"]
            except RequestError as exc:
                if job["continuous"] and not refused and is_usage_limit_error(exc.data):
                    return reschedule_after_quota(store, job, server, state, key)
                store.transition(job_id, state, "failed", error_text(exc))
                return 2
            store.transition(job_id, state, "accepted", f"Codex 已接收消息。Turn ID {turn_id}", turn_id=turn_id)
            state = "accepted"
            turn = server.wait_turn(thread_id, turn_id, timeout=RUN_LIMIT_SECONDS)
            reason = turn.get("error") or {}
            quota_hit = is_usage_limit_error(reason)
            if job["continuous"] and not refused and quota_hit:
                return reschedule_after_quota(store, job, server, state, key)
            if turn.get("status") == "completed" and not quota_hit:
                detail = "Codex 回合已完成。"
                if refused:
                    detail += " 本次执行有交互请求被拒绝，请看上方记录。"
                elif job["continuous"]:
                    detail += " 持续续跑结束。"
                store.transition(job_id, state, "review" if refused else "completed", detail)
                return 0
            detail = reason.get("message") or turn.get("status") or "未知状态"
            store.transition(job_id, state, "failed", "Codex 回合未完成。" + error_text(detail))
            return 2
    except Exception as exc:
        next_state = "failed" if state == "preparing" else "unknown"
        store.transition(job_id, state, next_state, error_text(exc))
        return 2

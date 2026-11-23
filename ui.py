"""Chinese-language Windows control panel."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from codex_client import AppServer, CodexError, Connection, VERSION, check_target, error_text
from jobs import DEFAULT_MESSAGE, RUNNING_STATES, scheduler, task_name, thread_title, validate_thread_id
from local_datetime import parse_local_time


BG = "#eff3f8"
PANEL = "#ffffff"
INK = "#162239"
MUTED = "#4f6078"
ACCENT = "#2959d6"
LINE = "#d6dfeb"
FONT = "Microsoft YaHei UI"
STATES = {
    "scheduled": "等待触发", "preparing": "正在连接", "submitting": "正在发送",
    "accepted": "已接收 · 执行中", "completed": "已完成", "review": "需查看日志",
    "failed": "失败", "cancelled": "已取消", "unknown": "结果待核对",
    "queued": "已入队 · 等待开跑", "queue_pending": "已入队 · 结果待核对",
    "rescheduled": "已安排下一轮", "stopped": "已停止续跑",
}


def local_time(value, fmt="%Y-%m-%d  %H:%M:%S"):
    try:
        return datetime.fromtimestamp(value).strftime(fmt)
    except (OSError, OverflowError, TypeError, ValueError):
        return "—"


class Window:
    def __init__(self, root, store, initial_thread):
        self.root = root
        self.store = store
        self.executable = store.setting("codex_path")
        self.worker_pool = ThreadPoolExecutor(max_workers=3)
        self.results = queue.Queue()
        self.in_progress = set()
        self.threads = {}
        self.jobs = {}
        self.missing = set()
        self.cursor = None
        self.active_search = ""
        self.quota = None
        self.root.title("Codex Auto Resume")
        self.root.minsize(1010, 650)
        width = min(1190, max(1010, root.winfo_screenwidth() - 40))
        height = min(760, max(650, root.winfo_screenheight() - 85))
        self.root.geometry(f"{width}x{height}")
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._styles()
        self._build(initial_thread)
        self.root.after(40, self._drain)
        self.root.after(100, self.load_threads)
        self.root.after(150, self.load_jobs)
        self.root.after(200, self.load_quota)
        self.root.after(5000, self.refresh_history)

    def _styles(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", font=(FONT, 10), foreground=INK, background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Panel.TLabel", background=PANEL, foreground=INK)
        style.configure("Heading.TLabel", background=PANEL, foreground=INK, font=(FONT, 13, "bold"))
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED, font=(FONT, 9))
        style.configure("TEntry", fieldbackground=PANEL, padding=6, bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
        style.configure("TButton", padding=(10, 6), background="#eef2f9", bordercolor=LINE, lightcolor=LINE, darkcolor=LINE)
        style.map("TButton", background=[("disabled", "#f2f5f9"), ("active", "#e1e9f9")],
                  foreground=[("disabled", "#75849b")])
        style.configure("Primary.TButton", background=ACCENT, foreground="white", padding=(15, 9),
                        font=(FONT, 10, "bold"), bordercolor=ACCENT, lightcolor=ACCENT, darkcolor=ACCENT)
        style.map("Primary.TButton", background=[("disabled", "#d7e2f7"), ("active", "#214abc")],
                  foreground=[("disabled", "#536782")])
        style.configure("TCheckbutton", background=PANEL, padding=2)
        style.map("TCheckbutton", background=[("active", PANEL)])
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", padding=(18, 8), background="#e1e8f1", bordercolor=LINE)
        style.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", ACCENT)])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, rowheight=34, bordercolor=LINE)
        style.configure("Treeview.Heading", font=(FONT, 9, "bold"), background="#edf2fa", padding=(7, 7), bordercolor=LINE)
        style.map("Treeview", background=[("selected", "#dbe7ff")], foreground=[("selected", "#142f68")])

    def _build(self, initial_thread):
        header = tk.Frame(self.root, bg="#172643", padx=26, pady=17)
        header.pack(fill="x")
        tk.Label(header, text="Codex Auto Resume", font=(FONT, 19, "bold"),
                 fg="#ffffff", bg="#172643").pack(side="left")
        tk.Label(header, text="会话定时续跑", font=(FONT, 10),
                 fg="#c5d4ed", bg="#172643", padx=15).pack(side="left", pady=(5, 0))
        settings = tk.Menubutton(header, text="Codex 设置  ▾", font=(FONT, 10),
                                 relief="flat", bg="#2a3b5e", fg="white", padx=12, pady=5,
                                 activebackground="#395481", activeforeground="white")
        settings.pack(side="right")
        menu = tk.Menu(settings, tearoff=False)
        menu.add_command(label="选择 codex.exe…", command=self.choose_executable)
        menu.add_command(label="恢复自动查找", command=lambda: self.set_executable(""))
        menu.add_command(label="查看当前配置", command=self.show_config)
        menu.add_separator()
        menu.add_command(label="打开本地数据目录", command=self.open_data_dir)
        settings.configure(menu=menu)

        self.notice = tk.StringVar(value="正在读取本机 Codex 会话…")
        ttk.Label(self.root, textvariable=self.notice, foreground=MUTED,
                  anchor="w", padding=(24, 0, 24, 11)).pack(side="bottom", fill="x")
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=22, pady=(16, 10))
        self.form = ttk.Frame(self.notebook, padding=(0, 10, 0, 0))
        self.history = ttk.Frame(self.notebook, padding=(0, 10, 0, 0))
        self.notebook.add(self.form, text="新建计划")
        self.notebook.add(self.history, text="任务记录")
        self.notebook.bind("<<NotebookTabChanged>>", self._tab_changed)
        self._build_form(initial_thread)
        self._build_history()

    def _table(self, parent, columns, height):
        frame = ttk.Frame(parent, style="Panel.TFrame")
        tree = ttk.Treeview(frame, show="headings", selectmode="browse", height=height,
                            columns=tuple(col[0] for col in columns))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        for name, label, width, stretch in columns:
            tree.heading(name, text=label, anchor="w")
            tree.column(name, width=width, minwidth=90, stretch=stretch)
        tree.tag_configure("alt", background="#f7f9fd")
        scroll.pack(side="right", fill="y")
        tree.pack(side="left", fill="both", expand=True)
        return frame, tree

    @staticmethod
    def _text(parent, height):
        return tk.Text(parent, width=40, height=height, wrap="word", undo=True, padx=9, pady=7,
                       relief="flat", bg=PANEL, fg=INK, insertbackground=INK,
                       highlightbackground=LINE, highlightcolor=ACCENT, highlightthickness=1,
                       selectbackground="#dbe7ff", selectforeground=INK, font=(FONT, 10))

    def _build_form(self, initial_thread):
        self.form.rowconfigure(0, weight=1)
        self.form.columnconfigure(0, weight=3, minsize=535)
        self.form.columnconfigure(1, weight=2, minsize=402)
        left = ttk.Frame(self.form, style="Panel.TFrame", padding=16)
        right = ttk.Frame(self.form, style="Panel.TFrame", padding=17)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        right.grid(row=0, column=1, sticky="nsew")

        left_footer = ttk.Frame(left, style="Panel.TFrame")
        left_footer.pack(side="bottom", fill="x")
        ttk.Label(left, text="选择会话", style="Heading.TLabel").pack(anchor="w")
        search_box = ttk.Frame(left, style="Panel.TFrame")
        search_box.pack(fill="x", pady=(11, 9))
        self.search = tk.StringVar()
        search_entry = ttk.Entry(search_box, textvariable=self.search)
        search_entry.pack(side="left", fill="x", expand=True)
        search_entry.bind("<Return>", lambda _: self.load_threads())
        self.search_btn = ttk.Button(search_box, text="搜索标题", command=self.load_threads)
        self.search_btn.pack(side="left", padx=(7, 0))
        ttk.Button(search_box, text="刷新", command=self.reset_search).pack(side="left", padx=(6, 0))
        table, self.thread_list = self._table(left, (
            ("title", "会话标题", 240, True), ("updated", "最近更新", 105, False), ("cwd", "工作目录", 135, True),
        ), 5)
        table.pack(fill="both", expand=True)
        self.thread_list.bind("<<TreeviewSelect>>", self._thread_selected)
        list_footer = ttk.Frame(left_footer, style="Panel.TFrame")
        list_footer.pack(fill="x", pady=(7, 5))
        self.list_status = tk.StringVar(value="正在读取…")
        ttk.Label(list_footer, textvariable=self.list_status, style="Muted.TLabel").pack(side="left")
        self.more_btn = ttk.Button(list_footer, text="加载更多", command=lambda: self.load_threads(more=True), state="disabled")
        self.more_btn.pack(side="right")
        ttk.Label(left_footer, text="会话 ID 或 Codex 会话链接", style="Panel.TLabel").pack(anchor="w")
        id_row = ttk.Frame(left_footer, style="Panel.TFrame")
        id_row.pack(fill="x", pady=(5, 0))
        self.thread_id = tk.StringVar(value=initial_thread)
        ttk.Entry(id_row, textvariable=self.thread_id).pack(side="left", fill="x", expand=True)
        self.check_btn = ttk.Button(id_row, text="只读验证", command=self.verify)
        self.check_btn.pack(side="left", padx=(8, 0))
        self.thread_id.trace_add("write", self._target_changed)
        self._build_quota(left_footer)

        actions = ttk.Frame(right, style="Panel.TFrame")
        actions.pack(side="bottom", fill="x")
        ttk.Label(right, text="发送设置", style="Heading.TLabel").pack(anchor="w")
        self.selection = tk.StringVar(value="从列表选择会话，或粘贴会话 ID。")
        select_label = ttk.Label(right, textvariable=self.selection, style="Muted.TLabel", wraplength=360)
        select_label.pack(fill="x", pady=(8, 12))
        right.bind("<Configure>", lambda event: select_label.configure(wraplength=max(160, event.width - 36)))
        ttk.Label(right, text="发送时间（Windows 本地时间）", style="Panel.TLabel").pack(anchor="w")
        self.when = tk.StringVar()
        ttk.Entry(right, textvariable=self.when).pack(fill="x", pady=(5, 6))
        times = ttk.Frame(right, style="Panel.TFrame")
        times.pack(fill="x", pady=(0, 13))
        for minutes, label in ((15, "15 分钟后"), (60, "1 小时后"), (180, "3 小时后")):
            ttk.Button(times, text=label, command=lambda n=minutes: self.set_time(n)).pack(side="left", padx=(0, 6))
        self.set_time(60)
        message_row = ttk.Frame(right, style="Panel.TFrame")
        message_row.pack(fill="x")
        ttk.Label(message_row, text="发送内容", style="Panel.TLabel").pack(side="left")
        ttk.Button(message_row, text="恢复默认", command=self.set_default_prompt).pack(side="right")
        self.prompt = self._text(right, 3)
        self.prompt.pack(fill="both", expand=True, pady=(5, 9))
        self.set_default_prompt()
        self.continuous = tk.BooleanVar(value=False)
        ttk.Checkbutton(actions, text="持续续跑：因额度用尽中断时，等额度恢复后继续",
                        variable=self.continuous, command=self._set_plan_label).pack(anchor="w")
        self.wake = tk.BooleanVar(value=False)
        ttk.Checkbutton(actions, text="允许 Windows 尝试从睡眠唤醒", variable=self.wake).pack(anchor="w")
        ttk.Label(actions, style="Muted.TLabel", text="锁屏后仍可触发。请保持 Windows 登录及目标会话空闲。",
                  wraplength=350).pack(anchor="w", pady=(4, 9))
        self.create_btn = ttk.Button(actions, text="创建一次性计划", style="Primary.TButton", command=self.create_job)
        self.create_btn.pack(fill="x")
        if initial_thread:
            self._target_changed()

    def _build_quota(self, parent):
        quota = ttk.Frame(parent, style="Panel.TFrame")
        quota.pack(fill="x", pady=(10, 0))
        ttk.Separator(quota).pack(fill="x", pady=(0, 7))
        content = ttk.Frame(quota, style="Panel.TFrame")
        content.pack(fill="x")
        self.quota_text = tk.StringVar(value="正在读取 Codex 额度…\n")
        ttk.Label(content, textvariable=self.quota_text, style="Muted.TLabel", wraplength=250).pack(side="left", fill="x", expand=True)
        buttons = ttk.Frame(content, style="Panel.TFrame")
        buttons.pack(side="right", padx=(7, 0))
        self.use_quota_btn = ttk.Button(buttons, text="用额度恢复后 1 分钟", command=self.use_quota_time, state="disabled")
        self.use_quota_btn.pack(side="left")
        self.refresh_quota_btn = ttk.Button(buttons, text="刷新额度", command=self.load_quota)
        self.refresh_quota_btn.pack(side="left", padx=(6, 0))

    def _build_history(self):
        panel = ttk.Frame(self.history, style="Panel.TFrame", padding=16)
        panel.pack(fill="both", expand=True)
        header = ttk.Frame(panel, style="Panel.TFrame")
        header.pack(fill="x", pady=(0, 10))
        ttk.Label(header, text="计划与执行记录", style="Heading.TLabel").pack(side="left")
        self.cancel_btn = ttk.Button(header, text="取消选中的计划", command=self.cancel_job, state="disabled")
        self.cancel_btn.pack(side="right")
        ttk.Button(header, text="刷新", command=lambda: self.load_jobs(check_scheduler=True)).pack(side="right", padx=7)
        table, self.job_list = self._table(panel, (
            ("time", "发送时间", 170, False), ("title", "目标会话", 390, True),
            ("state", "状态", 152, False), ("updated", "状态更新", 174, False),
        ), 6)
        table.pack(fill="both", expand=True)
        self.job_list.bind("<<TreeviewSelect>>", lambda _: self.show_job_details())
        details = ttk.Frame(panel, style="Panel.TFrame")
        details.pack(fill="x", pady=(10, 5))
        ttk.Label(details, text="执行详情", style="Panel.TLabel").pack(side="left")
        ttk.Button(details, text="复制会话 ID", command=self.copy_thread_id).pack(side="right")
        ttk.Button(details, text="打开 Codex 会话", command=self.open_thread).pack(side="right", padx=7)
        log_panel = ttk.Frame(panel, style="Panel.TFrame")
        log_panel.pack(fill="x")
        self.job_log = self._text(log_panel, 8)
        scrollbar = ttk.Scrollbar(log_panel, orient="vertical", command=self.job_log.yview)
        self.job_log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.job_log.pack(side="left", fill="x", expand=True)
        self._write_log("选择一条计划以查看发送内容和执行日志。")

    def _submit(self, key, fn, done, failed=None):
        if key in self.in_progress:
            return
        self.in_progress.add(key)
        future = self.worker_pool.submit(fn)
        future.add_done_callback(lambda completed: self.results.put((key, completed, done, failed)))

    def _drain(self):
        while True:
            try:
                key, future, done, failed = self.results.get_nowait()
            except queue.Empty:
                break
            self.in_progress.discard(key)
            try:
                result = future.result()
            except Exception as exc:
                (failed or self.show_error)(exc)
            else:
                done(result)
        self.root.after(60, self._drain)

    def close(self):
        self.worker_pool.shutdown(wait=False, cancel_futures=True)
        self.root.destroy()

    def connection(self):
        return Connection.local(self.executable)

    def choose_executable(self):
        selected = filedialog.askopenfilename(parent=self.root, title="选择 Codex CLI", filetypes=[("Codex CLI", "codex.exe")])
        if selected:
            self.set_executable(selected)

    def set_executable(self, executable):
        if any(key in self.in_progress for key in ("threads", "check", "create", "quota")):
            self.notice.set("请等当前 Codex 操作完成后修改设置。")
            return
        self.store.set_setting("codex_path", executable)
        self.executable = executable
        self.load_threads()
        self.load_quota()

    def show_config(self):
        connection = self.connection()
        try:
            binary = str(connection.resolve_executable())
        except Exception as exc:
            binary = error_text(exc)
        messagebox.showinfo(f"Codex Auto Resume {VERSION}", f"Codex CLI\n{binary}\n\nCODEX_HOME\n{connection.home}", parent=self.root)

    def set_default_prompt(self):
        self.prompt.delete("1.0", "end")
        self.prompt.insert("1.0", DEFAULT_MESSAGE)

    def set_time(self, minutes):
        self.when.set((datetime.now() + timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M"))

    def load_quota(self):
        if "quota" in self.in_progress:
            return
        connection = self.connection()
        self.quota = None
        self.quota_text.set("正在读取 Codex 额度…\n")
        self.refresh_quota_btn.state(["disabled"])
        self.use_quota_btn.state(["disabled"])

        def get():
            with AppServer(connection) as server:
                return server.read_quota()

        def done(snapshot):
            self.quota = snapshot
            rows = []
            for name, window in (("5 小时额度", snapshot.five_hours), ("周额度", snapshot.weekly)):
                if window is None:
                    rows.append(f"{name} · 暂无数据")
                    continue
                used = f"已用 {window.used_percent}%" if window.used_percent < 100 else "已用完"
                reset = local_time(window.resets_at, "%m-%d %H:%M:%S")
                rows.append(f"{name} · {used} · 恢复 {reset}")
            self.quota_text.set("\n".join(rows))
            self.refresh_quota_btn.state(["!disabled"])
            self.use_quota_btn.state(["!disabled" if snapshot.five_hours or snapshot.weekly else "disabled"])

        def failed(error):
            self.quota_text.set("额度读取失败。可刷新，或手动输入发送时间。\n")
            self.notice.set(error_text(error)[:145])
            self.refresh_quota_btn.state(["!disabled"])

        self._submit("quota", get, done, failed)

    def use_quota_time(self):
        if self.quota is None:
            return
        try:
            next_run = self.quota.resume_time(datetime.now().timestamp())
            display = local_time(next_run, "%Y-%m-%d %H:%M:%S")
            parse_local_time(display)
        except (CodexError, ValueError) as exc:
            self.notice.set(error_text(exc))
            return
        self.when.set(display)
        self.notice.set(f"发送时间已填入：{display}（本地时间）")

    def _set_plan_label(self):
        self.create_btn.configure(text="创建持续续跑计划" if self.continuous.get() else "创建一次性计划")

    def reset_search(self):
        self.search.set("")
        self.load_threads()

    def load_threads(self, more=False):
        if "threads" in self.in_progress:
            return
        cursor = self.cursor if more else None
        search = self.active_search if more else self.search.get().strip()
        connection = self.connection()
        self.list_status.set("正在读取会话…")
        self.search_btn.state(["disabled"])
        self.more_btn.state(["disabled"])

        def get():
            with AppServer(connection) as server:
                return server.list_threads(cursor=cursor, search=search)

        def done(result):
            if not more:
                self.thread_list.delete(*self.thread_list.get_children())
                self.threads.clear()
            for thread in result.get("data", []):
                key = thread["id"]
                if key in self.threads:
                    continue
                self.threads[key] = thread
                cwd = thread.get("cwd") or ""
                self.thread_list.insert("", "end", iid=key, tags=("alt",) if len(self.threads) % 2 == 0 else (), values=(
                    thread_title(thread), local_time(thread.get("updatedAt"), "%m-%d %H:%M"), Path(cwd).name or cwd,
                ))
            self.active_search = search
            self.cursor = result.get("nextCursor")
            self.search_btn.state(["!disabled"])
            self.more_btn.state(["!disabled" if self.cursor else "disabled"])
            self.list_status.set(f"已加载 {len(self.threads)} 个会话" if self.threads else "未找到会话，可手动输入 ID。")
            self.notice.set("已连接本机 Codex。")

        def failed(error):
            self.search_btn.state(["!disabled"])
            self.more_btn.state(["!disabled" if self.cursor else "disabled"])
            self.list_status.set("会话读取失败")
            self.notice.set(error_text(error)[:125])
            self.show_error(error)

        self._submit("threads", get, done, failed)

    def _thread_selected(self, _):
        selected = self.thread_list.selection()
        if selected:
            self.thread_id.set(selected[0])

    def _target_changed(self, *_):
        value = self.thread_id.get().strip()
        selected = self.threads.get(value)
        if selected:
            self.selection.set(f"{thread_title(selected)[:100]}\n{selected.get('cwd', '')}")
        else:
            self.selection.set("已输入会话 ID。可验证以查看标题和工作目录。" if value else "从列表选择会话，或粘贴会话 ID。")

    def verify(self):
        try:
            target = validate_thread_id(self.thread_id.get())
        except ValueError as exc:
            self.show_error(exc)
            return
        connection = self.connection()
        self.notice.set("正在只读验证目标会话…")
        self.check_btn.state(["disabled"])

        def done(thread):
            self.check_btn.state(["!disabled"])
            self.notice.set(f"已验证 {target} · 未发送消息。")
            try:
                if target == validate_thread_id(self.thread_id.get()):
                    self.selection.set(f"{thread_title(thread)[:100]}\n{thread['cwd']}")
            except ValueError:
                pass

        def failed(error):
            self.check_btn.state(["!disabled"])
            self.notice.set("目标会话验证失败。")
            self.show_error(error)

        self._submit("check", lambda: check_target(connection, target), done, failed)

    def create_job(self):
        try:
            target = validate_thread_id(self.thread_id.get())
            scheduled_at = parse_local_time(self.when.get())
            message = self.prompt.get("1.0", "end-1c")
            if not message.strip():
                raise ValueError("请输入发送内容，或点击恢复默认。")
            if len(message) > 16000:
                raise ValueError("发送内容最多 16,000 个字符。")
        except ValueError as exc:
            self.show_error(exc)
            return
        connection, wake, continuous = self.connection(), self.wake.get(), self.continuous.get()
        self.create_btn.state(["disabled"])
        self.notice.set("正在验证目标会话…")

        def verified(thread):
            excerpt = message if len(message) <= 350 else message[:350] + "…"
            run_mode = "持续续跑 · 仅在额度用尽后安排下一轮" if continuous else "一次性发送"
            confirmed = messagebox.askokcancel(
                "确认计划", f"{thread_title(thread)[:140]}\n{target}\n{thread['cwd']}\n\n"
                f"首次发送：{local_time(scheduled_at)}（本地时间）\n{run_mode}\n\n{excerpt}", parent=self.root,
            )
            if not confirmed:
                self.create_btn.state(["!disabled"])
                self.notice.set("已放弃创建计划。")
                return
            self.notice.set("正在注册 Windows 计划任务…")
            self._submit("create", lambda: self.store.schedule(connection, thread, message, scheduled_at, wake, continuous), done, failed)

        def done(job_id):
            self.create_btn.state(["!disabled"])
            self.notebook.select(self.history)
            self.notice.set(f"已创建{'持续续跑' if continuous else '一次性'}计划 · {local_time(scheduled_at)}")
            self.load_jobs(select=job_id)

        def failed(error):
            self.create_btn.state(["!disabled"])
            self.notice.set("创建计划失败。")
            self.show_error(error)

        self._submit("create", lambda: check_target(connection, target), verified, failed)

    def _tab_changed(self, _):
        if self.notebook.select() == str(self.history):
            self.load_jobs(check_scheduler=True)

    def refresh_history(self):
        if self.notebook.select() == str(self.history):
            self.load_jobs()
        self.root.after(5000, self.refresh_history)

    def load_jobs(self, select=None, check_scheduler=False):
        if "jobs" in self.in_progress:
            if select:
                self.root.after(150, lambda: self.load_jobs(select=select))
            return

        def get():
            self.store.reconcile_workers()
            rows = self.store.all()
            if not check_scheduler:
                return rows, None
            existing = scheduler("list")
            absent = {row["id"] for row in rows if row["state"] == "scheduled" and task_name(row["id"]) not in existing}
            return rows, absent

        def done(result):
            rows, absent = result
            previous_selection = self.current_job_id()
            previous_record = self.jobs.get(previous_selection)
            previous_missing = previous_selection in self.missing
            if absent is not None:
                self.missing = absent
            selected = select or previous_selection
            self.jobs = {row["id"]: row for row in rows}
            for index, job in enumerate(rows):
                job_id = job["id"]
                status = STATES.get(job["state"], job["state"])
                if job["state"] == "scheduled" and job_id in self.missing:
                    status = "Windows 任务缺失"
                fields = (local_time(job["scheduled_at"]), job["title"], status, local_time(job["updated_at"]))
                if not self.job_list.exists(job_id):
                    self.job_list.insert("", index, iid=job_id, values=fields)
                self.job_list.item(job_id, values=fields, tags=("alt",) if index % 2 else ())
            if selected and self.job_list.exists(selected):
                if selected != previous_selection:
                    self.job_list.selection_set(selected)
                    self.job_list.see(selected)
                elif self.jobs[selected] != previous_record or (selected in self.missing) != previous_missing:
                    self.show_job_details()
            elif not rows:
                self._write_log("还没有计划。请在「新建计划」中选择一个 Codex 会话。")

        self._submit("jobs", get, done)

    def _write_log(self, value):
        if value == self.job_log.get("1.0", "end-1c"):
            return
        self.job_log.configure(state="normal")
        self.job_log.delete("1.0", "end")
        self.job_log.insert("1.0", value)
        self.job_log.configure(state="disabled")

    def current_job_id(self):
        selected = self.job_list.selection()
        return selected[0] if selected else None

    def show_job_details(self):
        job_id = self.current_job_id()
        job = self.jobs.get(job_id)
        if not job:
            self.cancel_btn.state(["disabled"])
            return
        can_stop = job["continue_enabled"] and job["state"] in RUNNING_STATES
        self.cancel_btn.configure(text="停止后续续跑" if can_stop else "取消选中的计划")
        self.cancel_btn.state(["!disabled" if job["state"] == "scheduled" or can_stop else "disabled"])
        if "details" in self.in_progress:
            return

        def done(events):
            if job_id != self.current_job_id():
                self.show_job_details()
                return
            mode = "持续续跑" if job["continuous"] else "一次性发送"
            lines = [f"会话  {job['thread_id']}", f"目录  {job['cwd']}", f"方式  {mode}", "", job["message"], ""]
            if job_id in self.missing and job["state"] == "scheduled":
                lines.append("Windows 中没有找到对应的计划任务。")
            lines.extend(f"{local_time(event['at'])}  {event['detail']}" for event in events)
            self._write_log("\n".join(lines))

        self._submit("details", lambda: self.store.events(job_id), done)

    def cancel_job(self):
        job_id = self.current_job_id()
        job = self.jobs.get(job_id)
        if job is None:
            return
        if job["state"] == "scheduled":
            action, title = self.store.cancel, "取消计划"
            question = f"取消 {local_time(job['scheduled_at'])} 发送到「{job['title'][:80]}」的计划？"
        elif job["continue_enabled"] and job["state"] in RUNNING_STATES:
            action, title = self.store.stop_followups, "停止后续续跑"
            question = "让当前回合正常结束，并停止安排后续轮次？"
        else:
            return
        if not messagebox.askyesno(title, question, parent=self.root):
            return
        self.cancel_btn.state(["disabled"])

        def finished(_=None):
            self.load_jobs(select=job_id)

        def failed(error):
            self.cancel_btn.state(["!disabled"])
            finished()
            self.show_error(error)

        self._submit("cancel", lambda: action(job_id), finished, failed)

    def copy_thread_id(self):
        job = self.jobs.get(self.current_job_id())
        if job:
            self.root.clipboard_clear()
            self.root.clipboard_append(job["thread_id"])
            self.notice.set("已复制会话 ID。")

    def open_thread(self):
        job = self.jobs.get(self.current_job_id())
        if job:
            try:
                os.startfile(f"codex://threads/{validate_thread_id(job['thread_id'])}")
            except (ValueError, OSError) as exc:
                self.show_error(exc)

    def open_data_dir(self):
        os.startfile(self.store.root)

    def show_error(self, error):
        messagebox.showerror("Codex Auto Resume", error_text(error), parent=self.root)


def launch(store, initial_thread):
    root = tk.Tk()
    Window(root, store, initial_thread)
    # The optional Codex plugin uses this to find the stable copy of the application.
    temporary = None
    try:
        registration = {
            "executable": str(Path(sys.executable).with_name("pythonw.exe")),
            "script": str(Path(__file__).with_name("main.pyw").resolve()),
        }
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=store.root, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(registration, handle)
        temporary.replace(store.root / "launcher.json")
    except OSError:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    root.mainloop()

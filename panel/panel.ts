import { App, applyDocumentTheme, applyHostStyleVariables } from "@modelcontextprotocol/ext-apps";

type WindowQuota = { used_percent: number; resetLocal: string | null };
type Quota = { five_hours: WindowQuota | null; weekly: WindowQuota | null;
  suggestion: { epoch: number; local: string } | null; reason: string | null };
type Job = { id: string; state: string; scheduledLocal: string; continuous: boolean; action: "cancel" | "stop" | null };
type Bound = { state: "bound"; title: string; id: string } | { state: "unbound"; reason: string };
type Draft = { request_id: string; mode: "quota" | "manual"; when: string | number;
  message: string; wake: boolean; continuous: boolean };
type Created = { state: "created"; job: Job } | { state: "quota_changed"; quota: Quota };
type JobEvents = { message: string; events: { at: string; detail: string }[] };

const states: Record<string, string> = {
  scheduled: "等待触发", preparing: "正在连接", submitting: "正在发送", accepted: "执行中",
  completed: "已完成", review: "需查看日志", failed: "失败", cancelled: "已取消",
  unknown: "结果待核对", rescheduled: "已安排下一轮", stopped: "已停止续跑",
};
const el = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;
const input = (id: string) => el<HTMLInputElement>(id);
const button = (id: string) => el<HTMLButtonElement>(id);
const dialog = el<HTMLDialogElement>("confirmation");
const app = new App({ name: "Codex Auto Resume", version: "1.2.2" });
let key = "";
let bindingRevision = 0;
let jobsRevision = 0;
let quota: Quota | null = null;
let draft: Draft | null = null;
let confirmAction: (() => Promise<void>) | null = null;

function notice(message: string, error = false) {
  el("notice").textContent = message;
  el("notice").hidden = !message;
  el("notice").classList.toggle("error", error);
}

function explain(error: unknown) {
  return error instanceof Error ? error.message : String(error);
}

async function call<T>(name: string, args: Record<string, unknown> = {}) {
  const result = await app.callServerTool({ name, arguments: { key, ...args } });
  if (result.isError) {
    const error = result.content?.find(item => item.type === "text");
    throw new Error(error?.text ?? "操作失败，请重试。");
  }
  if (!result.structuredContent) throw new Error("未收到面板数据。请重新打开面板。");
  return result.structuredContent as T;
}

function updateForm() {
  const manual = input("manual").checked;
  el("suggested-time").hidden = manual;
  input("when").hidden = !manual;
  input("when").required = manual;
  button("review").disabled = manual ? !input("when").value : !quota?.suggestion;
}

function renderQuota(result: Quota) {
  quota = result;
  for (const [prefix, window] of [["short", result.five_hours], ["week", result.weekly]] as const) {
    const exhausted = (window?.used_percent ?? 0) >= 100;
    el(`${prefix}-text`).textContent = !window ? "暂不可用" : exhausted ? "已用完" : `已用 ${window.used_percent}%`;
    const bar = el<HTMLProgressElement>(`${prefix}-bar`);
    bar.value = window?.used_percent ?? 0;
    bar.hidden = !window;
    bar.closest(".quota-tile")?.toggleAttribute("data-exhausted", exhausted);
    el(`${prefix}-reset`).textContent = window?.resetLocal ? `恢复于 ${window.resetLocal}` : "";
  }
  el("suggestion").textContent = result.suggestion?.local ?? "暂无可用时间";
  el("quota-reason").textContent = result.reason;
  el("quota-reason").hidden = !result.reason;
  input("auto").disabled = !result.suggestion;
  updateForm();
}

async function refreshQuota(revision = bindingRevision) {
  button("refresh").disabled = true;
  try {
    const result = await call<Quota>("resume_quota");
    if (revision === bindingRevision) renderQuota(result);
  } catch (error) {
    if (revision === bindingRevision) renderQuota({ five_hours: null, weekly: null, suggestion: null, reason: explain(error) });
  } finally {
    if (revision === bindingRevision) button("refresh").disabled = false;
  }
}

function setConfirmation(title: string, description: string, label: string, action: () => Promise<void>) {
  el("confirm-title").textContent = title;
  el("confirm-detail").textContent = description;
  el("confirm-error").hidden = true;
  button("confirm").textContent = label;
  confirmAction = action;
  if (!dialog.open) dialog.showModal();
}

function stopJob(job: Job) {
  const label = job.action === "cancel" ? "取消计划" : "停止后续续跑";
  const revision = bindingRevision;
  setConfirmation(label, `${el("title").textContent}\n${job.scheduledLocal}\n${job.action === "stop" ? "当前回合会正常结束。" : "确认取消这条等待中的计划？"}`, label, async () => {
    await call("resume_stop", { job_id: job.id, action: job.action });
    if (revision !== bindingRevision) return;
    dialog.close();
    notice("已停止后续发送。");
    await refreshJobs();
  });
}

function renderJobs(jobs: Job[]) {
  const next = jobs.filter(job => job.state === "scheduled").sort((a, b) => a.scheduledLocal.localeCompare(b.scheduledLocal))[0];
  const stoppable = next ?? jobs.find(job => job.action === "stop");
  const running = jobs.some(job => ["preparing", "submitting", "accepted"].includes(job.state));
  el("next-time").textContent = next?.scheduledLocal ?? (running ? "当前回合执行中" : "暂无计划");
  button("next-action").hidden = !stoppable;
  button("next-action").textContent = stoppable?.action === "stop" ? "停止后续" : "取消计划";
  button("next-action").onclick = stoppable ? () => stopJob(stoppable) : null;

  const list = el("jobs");
  const existing = new Map(Array.from(list.children, child => [(child as HTMLElement).dataset.id, child]));
  const rows = jobs.map(job => {
    const row = existing.get(job.id) as HTMLLIElement | undefined;
    let item = row;
    if (!item) {
      item = document.createElement("li");
      item.dataset.id = job.id;
      const summary = document.createElement("div");
      summary.className = "row split";
      summary.append(document.createElement("span"), document.createElement("button"));
      const details = document.createElement("details");
      const caption = document.createElement("summary");
      caption.textContent = "执行日志";
      const log = document.createElement("pre");
      details.append(caption, log);
      details.ontoggle = () => { if (details.open) void loadEvents(job.id, log); };
      item.append(summary, details);
    }
    item.querySelector("span")!.textContent = `${job.scheduledLocal} · ${states[job.state] ?? job.state}${job.continuous ? " · 持续" : ""}`;
    const action = item.querySelector("button")!;
    action.hidden = !job.action;
    action.textContent = job.action === "stop" ? "停止后续" : "取消";
    action.onclick = job.action ? () => stopJob(job) : null;
    return item;
  });
  for (const child of existing.values()) if (!rows.includes(child as HTMLLIElement)) child.remove();
  rows.forEach((item, index) => { if (list.children[index] !== item) list.insertBefore(item, list.children[index] ?? null); });
  if (!rows.length) list.textContent = "暂无任务记录。";
  else if (list.firstChild?.nodeType === Node.TEXT_NODE) list.firstChild.remove();
}

async function loadEvents(jobId: string, target: HTMLElement) {
  const revision = bindingRevision;
  target.textContent = "读取中…";
  try {
    const result = await call<JobEvents>("resume_events", { job_id: jobId });
    if (revision === bindingRevision) target.textContent = [result.message, ...result.events.map(event => `${event.at}  ${event.detail}`)].join("\n\n");
  } catch (error) {
    if (revision === bindingRevision) target.textContent = explain(error);
  }
}

async function refreshJobs() {
  const revision = bindingRevision;
  const request = ++jobsRevision;
  try {
    const result = await call<{ jobs: Job[] }>("resume_jobs");
    if (revision === bindingRevision && request === jobsRevision) renderJobs(result.jobs);
  } catch (error) {
    if (revision === bindingRevision && request === jobsRevision) notice(explain(error), true);
  }
}

async function bind(link?: string) {
  const revision = ++bindingRevision;
  const form = el<HTMLFormElement>("binding");
  const submit = form.querySelector("button")!;
  submit.disabled = true;
  notice("正在核对会话…");
  try {
    const result = await call<Bound>("resume_bind", link === undefined ? {} : { link });
    if (revision !== bindingRevision) return;
    form.hidden = result.state === "bound";
    if (result.state === "unbound") {
      notice(result.reason, true);
      return;
    }
    el("title").textContent = result.title;
    el("title").title = result.id;
    el("workspace").hidden = false;
    notice("");
    await Promise.all([refreshQuota(revision), refreshJobs()]);
  } catch (error) {
    if (revision === bindingRevision) { form.hidden = false; notice(explain(error), true); }
  } finally {
    if (revision === bindingRevision) submit.disabled = false;
  }
}

function confirmDraft(current: Draft, shownTime: string) {
  const revision = bindingRevision;
  setConfirmation("确认续跑计划", `${el("title").textContent}\n${shownTime}（Windows 本地时间）\n${current.continuous ? "持续续跑" : "执行一次"}\n\n${current.message}`, "确认创建", async () => {
    const result = await call<Created>("resume_create", current);
    if (revision !== bindingRevision) return;
    if (result.state === "quota_changed") {
      renderQuota(result.quota);
      dialog.close();
      draft = null;
      notice(result.quota.suggestion ? "额度恢复时间已更新。请核对新时间后再确认。" : "额度时间暂不可用。请刷新额度或手动指定时间。", true);
      return;
    }
    dialog.close();
    draft = null;
    notice(`已创建计划 · ${result.job.scheduledLocal}`);
    await refreshJobs();
  });
}

el<HTMLFormElement>("create").onsubmit = event => {
  event.preventDefault();
  const manual = input("manual").checked;
  const suggestion = quota?.suggestion;
  if (!manual && !suggestion) return;
  const message = el<HTMLTextAreaElement>("message").value;
  if (!message.trim()) { notice("请输入发送内容。", true); return; }
  const when = manual ? input("when").value : suggestion!.epoch;
  const payload = { mode: manual ? "manual" as const : "quota" as const, when, message,
    continuous: input("continuous").checked, wake: input("wake").checked };
  const sameDraft = draft && Object.entries(payload).every(([k, value]) => draft?.[k as keyof Draft] === value);
  draft = { request_id: sameDraft ? draft!.request_id : crypto.randomUUID(), ...payload };
  confirmDraft(draft, manual ? String(when).replace("T", " ") : suggestion!.local);
};

button("dismiss").onclick = () => dialog.close();
dialog.oncancel = event => { if (button("confirm").disabled) event.preventDefault(); };
button("confirm").onclick = async () => {
  if (!confirmAction) return;
  const revision = bindingRevision;
  button("confirm").disabled = button("dismiss").disabled = true;
  el("confirm-error").hidden = true;
  try { await confirmAction(); }
  catch (error) {
    if (revision === bindingRevision) { el("confirm-error").textContent = explain(error); el("confirm-error").hidden = false; }
  } finally {
    if (revision === bindingRevision) button("confirm").disabled = button("dismiss").disabled = false;
  }
};
el<HTMLFormElement>("binding").onsubmit = event => { event.preventDefault(); void bind(input("link").value.trim()); };
button("refresh").onclick = () => void refreshQuota();
input("auto").onchange = input("manual").onchange = input("when").oninput = updateForm;

function styleHost(context: ReturnType<typeof app.getHostContext>) {
  if (context?.theme) applyDocumentTheme(context.theme);
  if (context?.styles?.variables) applyHostStyleVariables(context.styles.variables);
}
app.onhostcontextchanged = () => styleHost(app.getHostContext());
app.ontoolresult = result => {
  const metadata = result._meta;
  if (typeof metadata?.binding !== "string" || metadata.binding === key) return;
  key = metadata.binding;
  quota = draft = null;
  el("workspace").hidden = el("binding").hidden = true;
  el("title").textContent = "Codex Auto Resume";
  el("title").title = "";
  confirmAction = null;
  button("confirm").disabled = button("dismiss").disabled = false;
  if (dialog.open) dialog.close();
  el<HTMLTextAreaElement>("message").value = String(metadata.defaultMessage ?? "");
  input("manual").checked = input("wake").checked = input("continuous").checked = false;
  input("auto").checked = true;
  input("when").value = "";
  renderJobs([]);
  void bind();
};
app.connect().then(() => styleHost(app.getHostContext())).catch(error => notice(explain(error), true));
setTimeout(() => { if (!key) notice("未收到会话面板数据。请从当前对话的右侧工具菜单重新打开。", true); }, 8000);
setInterval(() => { if (!document.hidden && !el("workspace").hidden) void refreshJobs(); }, 10000);

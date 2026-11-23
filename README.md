# Codex Auto Resume

[English guide](docs/README.en.md) · [下载最新版本](https://github.com/PanGucheng/CodexAutoResume-GUI/releases/latest) · [贡献指南](CONTRIBUTING.md) · [MIT 许可](LICENSE)

在 Windows 版 Codex 的对话侧栏里，安排一条消息在额度恢复后发回原会话。工具读取 **5 小时和周额度**，可以选择 **恢复后 1 分钟**或指定时间。开启持续续跑后，只有 Codex 明确报告额度用尽，才会登记下一轮。每轮原样发送你填写的内容。

这是一个独立社区项目。计划由 Windows 任务计划程序运行，Codex 应用窗口关闭后也可触发。

## 安装原生面板

1. 在 Windows 10/11 上安装并登录 Codex。确认 PowerShell 可以执行 `codex --version`、`py -3 --version` 和 `uv --version`。需要 Python 3.10 或更高版本。
2. 在[最新发布](https://github.com/PanGucheng/CodexAutoResume-GUI/releases/latest)下载 **Source code (zip)**，完整解压到以后会保留的位置。发布包内有构建好的面板。解压目录也是本地插件市场的来源。
3. 在解压后的项目根目录运行安装器。

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-Panel.ps1
   ```

4. 完全退出并重新打开 Codex。在一个已保存的本机会话里，点击右上角最右边的分栏图标，在右侧工作区点击 **新标签页 → 打开（Open）**，选择 **Codex Auto Resume**。如列表里没有它，展开 **Open** 右侧的 **···**。可以在这里固定面板入口。

## 创建与管理计划

1. 核对面板顶部的会话标题。若需要手动绑定，粘贴 `codex://threads/<ID>` 后点击 **核对会话**。
2. 选择 **额度恢复后 1 分钟**，或输入 Windows 本地时间。两项额度均耗尽时使用较晚的恢复时间。两项都有余量时，额度快捷选项使用 5 小时窗口的恢复时间。
3. 填写发送内容，按需开启 **持续续跑**，核对会话、时间和内容后确认创建。
4. 在 **任务记录** 查看发送结果、取消待执行计划，或停止后续续跑。回合正常结束时，持续续跑随之结束。

工具也提供独立的 Python/Tk 窗口。双击 [Launch.cmd](Launch.cmd)，可选择本机会话并使用同一个任务库。此入口需要 Python 带 Tcl/Tk。找不到 Codex CLI 时，可在窗口的 **Codex 设置**里选择 `codex.exe`。

## 使用条件与排查

| 情况 | 处理方式 |
| --- | --- |
| 看不到原生面板 | 确认在已保存的本机会话右侧工作区。摘要里的「来源 → 使用插件」会把插件提及放入输入框。 |
| 到点没有发送 | 检查 Windows 用户是否仍登录，以及目标会话是否空闲。锁屏可以触发。睡眠唤醒取决于硬件和电源设置，登录后可补跑错过的计划。 |
| 额度恢复时间不可用 | 刷新额度或手动选择首次发送时间。如果额度中断后缺少所需的恢复时间，任务记录会说明原因。 |
| 需查看日志 | 查看是否有人工交互、目标变化或登记失败。运行中的 Codex 需要批准或输入时，后台会记录该请求。 |
| 结果待核对 | 先查看原会话是否已收到消息，再决定是否新建计划。发送后断连不会自动重试。 |

后台使用当前 Windows 用户的 Codex 登录与配置。提示词和执行记录保存在 `%LOCALAPPDATA%\CodexAutoResumeGUI\jobs.sqlite3`，本机计划任务名称以 `CodexAutoResumeGUI_` 开头。后台执行器位于同一数据目录下的 `panel-runtime`。升级或移动文件夹时，保留待执行计划仍引用的运行目录。

## 源码与复用

| 入口 | 用途 |
| --- | --- |
| [`codex_client.py`](codex_client.py) | App Server 的 stdio 客户端、额度读取和恢复时间计算 |
| [`jobs.py`](jobs.py)、[`task-scheduler.ps1`](task-scheduler.ps1) | SQLite 任务库、Windows 调度和续跑执行器 |
| [`panel/`](panel/) | MCP 服务、会话绑定与 TypeScript 原生面板 |
| [`plugins/codex-auto-resume/`](plugins/codex-auto-resume/) | 可安装的插件和生成的单文件面板 |
| [`tests/`](tests/) | 回归测试与可选的本机 App Server 验证 |

接口与数据流见[设计说明](DESIGN.md)，构建和测试见[贡献指南](CONTRIBUTING.md)。项目代码使用 [MIT](LICENSE)，面板内的第三方代码保留[原许可声明](THIRD_PARTY_NOTICES.txt)。

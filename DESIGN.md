# Codex Auto Resume 设计与实施方案

## 旧版行为

v1.1 的 PowerShell 脚本读取 `session_index.jsonl` 和近期会话日志，列出最多 25 个会话，也接受手动输入的 thread ID。它先用 `thread-owner-discovery` 检查 Desktop 是否加载目标，然后把时间、thread ID 和提示词写入本地 JSON，以当前 Windows 用户身份注册一次性计划任务。到点后，任务通过 `codex-ipc` 调用 `thread-follower-start-turn`。脚本写入发送结果，取消操作由用户在 Windows 任务计划程序中执行。

待替换的部分是 Desktop 私有管道及会话日志解析。定时、锁屏运行、一次发送、默认提示词、只读验证和可查的状态都属于新版本的验收范围。

## 方案选择

| 方案 | 评估 |
| --- | --- |
| Codex 内置计划任务 | 官方支持在当前对话周期性运行。公开文档未给出从统一界面选择任意本机旧会话并在指定时间只发送一次的受支持流程。 |
| 原生 Codex 插件 | 插件可提供技能、MCP 工具、工具返回的组件和生命周期钩子。计划触发仍需要本机执行器。采用本地技能插件作为图形窗口入口，确认与写入计划任务仍由窗口完成。 |
| `codex queue --resume` | 官方 CLI 能排队文本，自动恢复依赖可用的 daemon 或远端 app server。当前 Windows 上 `app-server proxy` 无法连接默认控制套接字，自动恢复失败也可能以成功退出。 |
| Windows 窗口 + App Server stdio | Windows 调度器独立于前端。App Server 通过公开的 `thread/list`、`thread/read`、`thread/resume` 和 `turn/start` 管理会话，`turn/completed` 可区分成功、失败和中断。选用此方案。 |

- [官方计划任务说明](https://learn.chatgpt.com/docs/automations?surface=app)
- [官方插件架构](https://developers.openai.com/plugins/concepts/plugins)
- [官方 CLI 命令](https://learn.chatgpt.com/docs/cli/reference#codex-queue)
- [官方 App Server 协议](https://learn.chatgpt.com/docs/app-server)

## 实施范围

1. **会话选择与验证。** Python/Tk 中文窗口从 App Server 加载本机近期可见会话，支持搜索、翻页及手动输入 ID。只读验证检查该会话和它的工作目录，显示登录及配置错误。选择会话后设置本机时间和文本，提交前确认摘要。
2. **持久化与调度。** 工具单独使用 `%LOCALAPPDATA%\CodexAutoResumeGUI` 下的 SQLite。Windows 任务以 `InteractiveToken` 和普通权限运行，支持用户已登录时锁屏触发、允许电池运行、可选唤醒与错过后补跑。命令行只传递任务 ID。取消先记录到本地状态，再注销 Windows 任务，防止竞争条件导致已取消任务发送。
3. **发送与结果。** 定时工作进程原地恢复目标会话，向 `turn/start` 提交一次用户文本并记录 turn ID，保持 App Server 生命周期直到回合完成或超时。状态区分等待、连接、发送中、已接收、完成、失败、已取消和结果待核对。对重复启动使用原子状态转换，对同一会话的本工具后台回合使用互斥。若请求提交后连接断开，保留可见的未知状态并停止自动重试。
4. **权限及并发。** 使用本机 Codex 已有登录和配置。后台执行器拒绝需要人工批准或介入的请求，并记录原因。App Server 是独立客户端，只能在发送前通过已存历史尽力识别正在进行的回合。使用者应让目标会话在预定时间空闲。Desktop 可能需要重新打开会话以刷新内容。
5. **交付。** 提供双击启动器、环境检测、独立执行入口、可选的工作区插件包、安装与故障排查说明。旧版任务记录与新调度分开，不做自动迁移，以免意外重发。

## 验收

- 在当前 Codex CLI 验证 stdio 握手、中文标题、会话列表和只读验证。
- 验证 Windows 计划任务注册、查询、真实触发和取消路径。用隔离任务检查重复触发、未知结果和日志。
- 用独立的临时 Codex 会话验证原 ID 续接和回合完成。
- 检查中文图形窗口及空态，验证工作区插件目录和启动入口。

本机 App Server 版本为 `codex-cli 0.167.0-alpha.4.1`。已验证最近会话和中文标题的读取、一次性计划的注册和取消、定时触发时在临时会话的原 ID 上写入一轮并完成，以及重复触发保护。插件通过隔离 `CODEX_HOME` 的实际安装验证。回归测试可运行 `py -3 -m unittest discover -s tests -v`。

## 额度快捷时间与持续续跑

App Server 的 `account/rateLimits/read` 提供两种额度窗口的使用率和 Unix 恢复时间。按窗口长度匹配普通 Codex 的 300 和 10,080 分钟窗口。所有已耗尽窗口都需恢复时，取较晚的恢复时间。UI 预设与续跑执行器共用这套计算，并统一增加 60 秒。

持续续跑沿用一次性 Windows 任务。新增的 `continuations` 表关联每轮与其上游任务，旧任务按原方式执行。每次只登记紧接的一轮，任务状态与后继任务在同一 SQLite 事务内写入。通过原会话的目标身份与状态，或当轮最终回复中的明确状态行，判断还有没有可继续的工作。结构化额度错误也可触发后续轮次。持续续跑的未决状态与失败原因可在任务记录中查看。

## 原生对话面板

插件在 `resume_open` 的工具元数据中声明 `thread` 入口。它通过 stdio MCP 提供独立面板绑定，TypeScript 界面使用 MCP Apps 桥直接读取本地服务。打开时用宿主提供的 `threadId` 或 `thread_id` 作为候选值，并用 App Server 核对。无可信候选值时允许粘贴本机会话链接。创建与取消等操作由面板内明确确认，查询和写入限定在已绑定的会话。

确认额度预设时重新读取额度数据，时间变化时由用户核对新值。SQLite 用提交 ID 和内容指纹记录已登记的计划。在同一事务内建立计划及提交记录，使重试返回已经登记的任务。安装器为 Python 后端和调度执行器写入稳定的版本目录，插件缓存中的 HTML 与启动入口可独立升级。

入口和主题桥参考 [OpenAI MCP Extensions](https://github.com/openai/mcp-extensions/blob/main/docs/spec.md#thread-entrypoint) 与 [MCP App Basics](https://github.com/openai/openai-apps-sdk-examples/tree/main/mcp_app_basics_node)。当前会话识别与本地插件打包参考 [Codex Trajectory](https://github.com/icesixgod/codex-trajectory/blob/main/docs/interface.md#native-viewer-entrypoint) 和 [GitHub Tasks](https://github.com/rafazafar/codex-gh-tasks-plugin#how-it-works)。

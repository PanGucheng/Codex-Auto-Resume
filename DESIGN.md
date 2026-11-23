# Codex Auto Resume 设计说明

## 旧版行为

v1.1 的 PowerShell 脚本读取 `session_index.jsonl` 和近期会话日志，列出最多 25 个会话，也接受手动输入的 thread ID。它先用 `thread-owner-discovery` 检查 Desktop 是否加载目标，然后把时间、thread ID 和提示词写入本地 JSON，以当前 Windows 用户身份注册一次性计划任务。到点后，任务通过 `codex-ipc` 调用 `thread-follower-start-turn`。脚本写入发送结果，取消操作由用户在 Windows 任务计划程序中执行。

待替换的部分是 Desktop 私有管道及会话日志解析。定时、锁屏运行、一次发送、默认提示词、只读验证和可查的状态都属于新版本的验收范围。

## 方案选择

| 方案 | 评估 |
| --- | --- |
| Codex 内置计划任务 | 官方支持在当前对话周期性运行。公开文档未给出从统一界面选择任意本机旧会话并在指定时间只发送一次的受支持流程。 |
| 原生 Codex 插件 | 本地 MCP 服务使用 `thread` 入口打开对话侧栏。创建及管理计划在面板内完成，Windows 调度器负责触发。 |
| Codex 消息队列 | 原对话沿用现有设置时默认使用。通过 `codex queue` 底层的 `thread/queue/add` 提交，持有对话的进程决定何时启动回合。 |
| App Server 直接执行 | 新建对话和指定模型或思考强度使用此模式。需要恢复已被桌面占用的原会话时，可以使用实验性的共享 App Server。 |

- [官方计划任务说明](https://learn.chatgpt.com/docs/automations?surface=app)
- [官方插件架构](https://developers.openai.com/plugins/concepts/plugins)
- [官方 CLI 命令](https://learn.chatgpt.com/docs/cli/reference#codex-queue)
- [官方 App Server 协议](https://learn.chatgpt.com/docs/app-server)

## 实施范围

1. **会话选择与验证。** Python/Tk 中文窗口从 App Server 加载本机近期可见会话，支持搜索、翻页及手动输入 ID。只读验证检查该会话和它的工作目录，显示登录及配置错误。选择会话后设置本机时间和文本，提交前确认摘要。
2. **持久化与调度。** 工具单独使用 `%LOCALAPPDATA%\CodexAutoResumeGUI` 下的 SQLite。Windows 任务以 `InteractiveToken` 和普通权限运行，支持用户已登录时锁屏触发、允许电池运行、可选唤醒与错过后补跑。命令行只传递任务 ID。取消先记录到本地状态，再注销 Windows 任务，防止竞争条件导致已取消任务发送。
3. **发送与结果。** 普通计划以计划 ID 作为 `clientUserMessageId` 入队。队列 RPC 需要 App Server 的 `experimentalApi` 能力，这也允许工具核对回执并通过 `thread/turns/list` 精确关联用户消息和回合，不依赖消息文本匹配。只有查到对应回合后才显示执行中，只有收到结构化额度错误才自动安排下一轮。直接执行模式通过 `thread/resume`、`turn/start` 和 `turn/completed` 追踪。两种方式都以原子状态转换防止计划重复触发，对同一会话的活跃任务使用互斥。提交后的模糊结果留给用户核对。
4. **权限及并发。** 使用本机 Codex 已有登录和配置。队列模式的权限及审批由原对话的宿主处理。直接执行模式记录后台拒绝的人工批准或输入。不同会话可以同时运行。队列由持有者消费，未加载或已被手动中断的对话可能继续等待。已有计划保留创建时使用的执行方式。
5. **交付。** 提供原生面板安装器、预构建插件包、双击启动器和故障排查说明。旧版计划任务仍由 Windows 任务计划程序单独管理。

## 验收

- 在当前 Codex CLI 验证 stdio 握手、中文标题、会话列表和只读验证。
- 验证 Windows 计划任务注册、查询、真实触发和取消路径。用隔离任务检查重复触发、未知结果和日志。
- 用独立的临时 Codex 会话验证原 ID 续接和回合完成。
- 检查中文图形窗口及空态，验证工作区插件目录和启动入口。

构建、回归测试和隔离 `CODEX_HOME` 的插件验证命令见[贡献指南](CONTRIBUTING.md)。

## 额度快捷时间与持续续跑

App Server 的 `account/rateLimits/read` 提供两种额度窗口的使用率和 Unix 恢复时间。按窗口长度匹配普通 Codex 的 300 和 10,080 分钟窗口。所有已耗尽窗口都需恢复时，取较晚的恢复时间。UI 预设与续跑执行器共用这套计算，并统一增加 60 秒。

持续续跑沿用一次性 Windows 任务。`continuations` 表关联每轮与其上游任务。每轮原样发送所填写的消息。收到本轮的结构化额度错误 `usageLimitExceeded` 时，后台才重新读取额度并登记下一轮。回合正常结束时，续跑结束。有会话目标时，发送前和安排下一轮前会核对目标身份和状态。任务状态与后继任务在同一 SQLite 事务内写入。未决状态和失败原因可在任务记录中查看。

## 原生对话面板

插件在 `resume_open` 的工具元数据中声明 `thread` 入口。它通过 stdio MCP 提供独立面板绑定，TypeScript 界面使用 MCP Apps 桥直接读取本地服务。打开时用宿主提供的 `threadId` 或 `thread_id` 作为候选值，并用 App Server 核对。无可信候选值时允许粘贴本机会话链接。创建与取消等操作由面板内明确确认，查询和写入限定在已绑定的会话。

确认额度预设时重新读取额度数据，时间变化时由用户核对新值。SQLite 用提交 ID 和内容指纹记录已登记的计划。在同一事务内建立计划及提交记录，使重试返回已经登记的任务。安装器为 Python 后端和调度执行器写入稳定的版本目录，插件缓存中的 HTML 与启动入口可独立升级。

入口和主题桥参考 [OpenAI MCP Extensions](https://github.com/openai/mcp-extensions/blob/main/docs/spec.md#thread-entrypoint) 与 [MCP App Basics](https://github.com/openai/openai-apps-sdk-examples/tree/main/mcp_app_basics_node)。当前会话识别与本地插件打包参考 [Codex Trajectory](https://github.com/icesixgod/codex-trajectory/blob/main/docs/interface.md#native-viewer-entrypoint) 和 [GitHub Tasks](https://github.com/rafazafar/codex-gh-tasks-plugin#how-it-works)。

# Codex Auto Resume

[中文](../README.md) · [Latest release](https://github.com/PanGucheng/CodexAutoResume-GUI/releases/latest) · [Contributing](../CONTRIBUTING.md) · [MIT license](../LICENSE)

Schedule a message back into an existing Codex conversation on Windows. The native side panel reads your five-hour and weekly quota windows and can schedule a send one minute after the applicable reset. With continuous resume enabled, a follow-up is scheduled only when Codex explicitly reports quota exhaustion. Every round sends the text you entered, verbatim.

This independent community project uses Windows Task Scheduler to run while the Codex window is closed. Keep your Windows user signed in and leave the target conversation idle at the scheduled time.

## Install the native panel

1. Sign in to Codex on Windows 10 or 11. In PowerShell, check `codex --version`, `py -3 --version` and `uv --version`. Python 3.10 or later is required.
2. Download **Source code (zip)** from the [latest release](https://github.com/PanGucheng/CodexAutoResume-GUI/releases/latest) and extract the entire archive into a permanent folder. It includes the built panel. Codex registers this folder as the source for the local plugin marketplace.
3. Run the installer from the extracted project root.

   ```powershell
   powershell -NoProfile -ExecutionPolicy Bypass -File .\Install-Panel.ps1
   ```

4. Quit and reopen Codex. Open a saved local conversation, click the rightmost split-pane button in its header, and open a new tab in the right workspace. Choose **Codex Auto Resume** under **Open** or use the **···** menu beside **Open** to find and pin it. The enclosed panel currently uses Chinese labels.

## Schedule a message

1. Verify the conversation title. If automatic binding is unavailable, paste the link `codex://threads/<ID>` and select **核对会话** (Verify conversation).
2. Pick **额度恢复后 1 分钟** (One minute after quota reset) or enter a local Windows date and time. When both windows are exhausted, the tool waits for the later reset. If both have capacity, the preset uses the five-hour reset.
3. Enter your message, choose whether to enable **持续续跑** (Continuous resume), and confirm the target, time and message. Check **任务记录** (Job history) to cancel a pending send or stop future rounds.

A normally finished round ends continuous resume. When Codex reports a usage limit, the worker reads the reset times again before scheduling the next one-time job. A saved conversation goal can stop a scheduled send if it is already complete, paused, or replaced.

For the standalone window, run [Launch.cmd](../Launch.cmd) with a Windows Python installation that includes Tcl/Tk. It shares the same job history as the native panel.

## Troubleshooting

- If the panel is missing, check the right workspace of a saved local conversation. The **Sources** menu inserts a plugin mention into the composer.
- While Windows is locked, scheduled sends can run with your user signed in. Wake from sleep depends on the PC and its power settings.
- If the job reports an unknown outcome, inspect the original conversation before creating another schedule. A send that disconnects after submission is left for review.
- Jobs use your local Codex sign-in and configuration. Prompts and results are stored in `%LOCALAPPDATA%\CodexAutoResumeGUI\jobs.sqlite3`. Keep versioned runtime folders that are still referenced by pending jobs.

## Reuse the code

Start with [the App Server client](../codex_client.py) for quota handling, [the scheduler and worker](../jobs.py) for job execution, or [the native panel source](../panel/) for the MCP integration. The [architecture notes](../DESIGN.md) describe how they fit together. See the [contribution guide](../CONTRIBUTING.md) for builds and tests, and the [third-party notices](../THIRD_PARTY_NOTICES.txt) for code bundled in the panel.

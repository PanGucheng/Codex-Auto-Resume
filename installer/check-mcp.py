"""Exercise the installed MCP launcher using only the packaged interpreter."""

import asyncio
import json
import os
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import win32api  # The packaged Windows DLLs must be discoverable.


async def main(runtime):
    plugin = runtime / "marketplace" / "plugins" / "codex-auto-resume"
    manifest = json.loads((plugin / "plugin.json").read_text(encoding="utf-8"))
    config = json.loads((plugin / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["autoresume-panel"]
    assert Path(config["command"]).samefile(sys.executable)
    assert Path(sys.executable).with_name("pythonw.exe").is_file()
    args = [arg.replace("${PLUGIN_ROOT}", str(plugin)) for arg in config["args"]]
    server = StdioServerParameters(command=config["command"], args=args, env=dict(os.environ))
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as client:
            assert (await client.initialize()).serverInfo.version == manifest["version"]
            entry = next(tool for tool in (await client.list_tools()).tools if tool.name == "resume_open")
            assert entry.model_dump(by_alias=True)["_meta"]["openai/ui"]["entrypoints"] == [{"type": "thread"}]
            content = await client.read_resource("ui://codex-auto-resume/panel.html")
            assert "<title>Codex Auto Resume</title>" in content.contents[0].text
            result = await client.call_tool("resume_open", {})
            assert not result.isError and result.model_dump(by_alias=True)["_meta"]["binding"]
    print("Packaged Python, native plugin metadata, and MCP startup passed.")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))

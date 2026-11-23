"""Local stdio MCP server for the Codex thread panel."""

import asyncio
from pathlib import Path
import sys

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server

from codex_client import error_text
from panel_service import PanelService


URI = "ui://codex-auto-resume/panel.html"
MIME = "text/html;profile=mcp-app"
UI_ONLY = {"ui": {"visibility": ["app"]}}
STRING = {"type": "string"}
BOOLEAN = {"type": "boolean"}


def descriptor(name, title, properties, required, *, write=False, opening=False):
    meta = UI_ONLY
    if opening:
        meta = {"ui": {"resourceUri": URI, "visibility": ["app"]},
                "openai/ui": {"entrypoints": [{"type": "thread"}]}}
    return types.Tool(
        name=name, title=title, description=title,
        inputSchema={"type": "object", "properties": properties, "required": required, "additionalProperties": False},
        annotations=types.ToolAnnotations(readOnlyHint=not write, openWorldHint=False), _meta=meta,
    )


def build_server(bundle, service):
    server = Server("codex-auto-resume", version="1.2.0")
    bound = {"key": STRING}
    job = {**bound, "job_id": STRING}
    tools = [
        descriptor("resume_open", "Codex Auto Resume", {}, [], opening=True),
        descriptor("resume_bind", "绑定会话", {**bound, "link": STRING}, ["key"]),
        descriptor("resume_quota", "读取额度", bound, ["key"]),
        descriptor("resume_jobs", "读取会话计划", bound, ["key"]),
        descriptor("resume_events", "查看任务日志", job, list(job)),
        descriptor("resume_create", "创建续跑计划", {
            **bound, "request_id": STRING, "mode": {"enum": ["quota", "manual"]},
            "when": {"type": ["string", "integer"]}, "message": STRING, "wake": BOOLEAN, "continuous": BOOLEAN,
        }, ["key", "request_id", "mode", "when", "message", "wake", "continuous"], write=True),
        descriptor("resume_stop", "停止续跑计划", {**job, "action": {"enum": ["stop", "cancel"]}}, [*job, "action"], write=True),
    ]
    handlers = {f"resume_{name}": getattr(service, name) for name in ("bind", "quota", "jobs", "events", "create", "stop")}

    @server.list_tools()
    async def list_tools():
        return tools

    @server.list_resources()
    async def list_resources():
        return [types.Resource(uri=URI, name="Codex Auto Resume", mimeType=MIME)]

    @server.list_resource_templates()
    async def list_resource_templates():
        return []

    @server.read_resource()
    async def read_resource(uri):
        if str(uri) != URI:
            raise ValueError("Unknown resource")
        return [ReadResourceContents(content=bundle, mime_type=MIME)]

    @server.call_tool()
    async def call_tool(name, arguments):
        try:
            if name == "resume_open":
                meta = server.request_context.meta
                payload = service.open(meta.model_dump(exclude_none=True) if meta else {})
                return types.CallToolResult(content=[types.TextContent(type="text", text="已打开续跑面板。")], _meta=payload)
            # Keep blocking App Server and Task Scheduler work off the MCP event loop.
            payload = await asyncio.to_thread(handlers[name], **arguments)
            return types.CallToolResult(content=[], structuredContent=payload)
        except Exception as exc:
            return types.CallToolResult(content=[types.TextContent(type="text", text=error_text(exc))], isError=True)

    return server


async def main(path):
    server = build_server(Path(path).read_text(encoding="utf-8"), PanelService())
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))

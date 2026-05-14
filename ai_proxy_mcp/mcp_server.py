"""FastMCP server factory.

We build a single FastMCP instance ("ai_proxy"), register the AI_Proxy tools
against it, and expose its streamable-HTTP ASGI app for uvicorn to serve.

Hermes Agent will see tools at ``http://<host>:<port>/mcp`` (the path is
fixed by FastMCP's default `streamable_http_path`).
"""
from __future__ import annotations

import logging

from mcp.server.fastmcp import FastMCP

from .tools import init_tools, register
from .ws_server import Bridge

logger = logging.getLogger(__name__)


def build_mcp_app(bridge: Bridge, *, wait_max_timeout: int = 120):
    """Construct the FastMCP server and return its ASGI app.

    Stateless + JSON response: recommended for production. We don't keep
    per-client session state — each tool call references the central
    DevicePool by task_id.
    """
    mcp = FastMCP(
        name="ai_proxy",
        stateless_http=True,
        json_response=True,
    )

    register(mcp)
    init_tools(bridge, wait_max_timeout=wait_max_timeout)

    logger.info(
        "MCP server built (tools: list_devices, get_device_status, "
        "start_task, wait_for_progress, get_task_status, cancel_task)"
    )
    return mcp.streamable_http_app()

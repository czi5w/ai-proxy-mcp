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


def build_mcp_app(bridge: Bridge):
    """Construct the FastMCP server and return its ASGI app.

    Stateless + JSON response: recommended for production, no per-client
    session state needed (each tool call is independent).
    """
    mcp = FastMCP(
        name="ai_proxy",
        stateless_http=True,
        json_response=True,
    )

    register(mcp)
    init_tools(bridge)

    logger.info(
        "MCP server built (tools: list_devices, run_on_device, get_device_status)"
    )
    return mcp.streamable_http_app()

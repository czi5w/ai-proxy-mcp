"""MCP tools backed by the AI_Proxy device pool.

These functions are exposed to Hermes Agent (or any MCP client) as:
    mcp_ai_proxy_list_devices
    mcp_ai_proxy_run_on_device
    mcp_ai_proxy_get_device_status

The leading "mcp_<server_name>_" prefix is added by Hermes automatically.
"""
from __future__ import annotations

import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .ws_server import Bridge

logger = logging.getLogger(__name__)

# Lazily-bound bridge reference. Set by `init_tools()` at server startup so
# the @mcp.tool functions can reach into the same DevicePool instance the
# WS server is writing to.
_bridge: Optional[Bridge] = None


def init_tools(bridge: Bridge) -> None:
    """Wire the tool functions to a live Bridge instance."""
    global _bridge
    _bridge = bridge


def _require_bridge() -> Bridge:
    if _bridge is None:
        raise ToolError("server not initialized")
    return _bridge


def register(mcp: FastMCP) -> None:
    """Attach the AI_Proxy tools to the given FastMCP instance."""

    @mcp.tool()
    def list_devices() -> list[str]:
        """List the device_id of every AI_Proxy currently connected.

        Returns an empty list if no devices are online. Always call this before
        ``run_on_device`` if you don't already know which devices exist.
        """
        return _require_bridge().pool.list_devices()

    @mcp.tool()
    async def run_on_device(device_id: str, prompt: str) -> str:
        """Run ``prompt`` on the specified device's local Copilot CLI and return
        the complete textual reply.

        This call blocks until the device finishes (or times out / errors). If
        the device is offline, returns an error to the LLM instead of blocking.

        Args:
            device_id: One of the IDs returned by ``list_devices``.
            prompt: Natural-language instruction to forward to that device.
        """
        bridge = _require_bridge()
        try:
            return await bridge.run_on_device(device_id, prompt)
        except TimeoutError:
            raise ToolError(
                f'device "{device_id}" did not finish within '
                f"{bridge.task_timeout}s"
            )
        except RuntimeError as exc:
            raise ToolError(str(exc))

    @mcp.tool()
    def get_device_status(device_id: str) -> dict:
        """Report whether a device is currently connected and whether it has
        an in-flight task.

        Returns a dict like ``{"connected": true, "busy": false}``.
        """
        bridge = _require_bridge()
        return {
            "connected": bridge.pool.is_connected(device_id),
            "busy": device_id in bridge.pool.busy_devices(),
        }

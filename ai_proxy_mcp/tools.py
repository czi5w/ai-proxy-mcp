"""MCP tools backed by the AI_Proxy device pool.

Exposed to Hermes as (with the standard ``mcp_<server>_`` prefix the client
adds automatically):

    mcp_ai_proxy_list_devices
    mcp_ai_proxy_get_device_status
    mcp_ai_proxy_start_task           — non-blocking, returns task_id
    mcp_ai_proxy_wait_for_progress    — long-poll until new chunk / done / timeout
    mcp_ai_proxy_get_task_status      — non-blocking snapshot
    mcp_ai_proxy_cancel_task          — soft cancel

Workflow for long-running coding tasks:

    1. start_task(device_id, prompt)            -> {task_id, ...}
    2. tell the user something like "已派给 X，跑起来了"
    3. loop:
         wait_for_progress(task_id, 30)         -> {accumulated_text, is_done, ...}
         relay the new content to the user
         break if is_done
    4. summarize the final accumulated_text
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from .ws_server import Bridge

logger = logging.getLogger(__name__)


# Lazily-bound bridge reference. Set by ``init_tools`` at server startup so
# the @mcp.tool functions can reach the live DevicePool the WS server writes
# to.
_bridge: Optional[Bridge] = None
_wait_max_timeout: int = 120


def init_tools(bridge: Bridge, wait_max_timeout: int = 120) -> None:
    global _bridge, _wait_max_timeout
    _bridge = bridge
    _wait_max_timeout = wait_max_timeout


def _require_bridge() -> Bridge:
    if _bridge is None:
        raise ToolError("server not initialized")
    return _bridge


def register(mcp: FastMCP) -> None:
    """Attach the AI_Proxy tools to the given FastMCP instance."""

    # ── Device introspection ────────────────────────────────────

    @mcp.tool()
    def list_devices() -> list[str]:
        """List the device_id of every AI_Proxy currently connected.

        Returns an empty list if no devices are online.
        """
        return _require_bridge().pool.list_devices()

    @mcp.tool()
    def get_device_status(device_id: str) -> dict:
        """Report whether a device is connected and whether it has any
        running task. Returns ``{"connected": bool, "busy": bool}``.
        """
        bridge = _require_bridge()
        return {
            "connected": bridge.pool.is_connected(device_id),
            "busy": device_id in bridge.pool.busy_devices(),
        }

    # ── Task lifecycle ──────────────────────────────────────────

    @mcp.tool()
    async def start_task(device_id: str, prompt: str) -> dict:
        """Asynchronously kick off a Copilot prompt on the named device.

        Returns *immediately* with the task_id. The task keeps running on the
        device; use ``wait_for_progress`` or ``get_task_status`` to observe
        it and ``cancel_task`` to stop it.

        Args:
            device_id: One of the IDs returned by ``list_devices``.
            prompt: Natural-language instruction for the device's Copilot CLI.
        """
        bridge = _require_bridge()
        try:
            task = await bridge.start(device_id, prompt)
        except RuntimeError as exc:
            raise ToolError(str(exc))
        return task.snapshot()

    @mcp.tool()
    async def wait_for_progress(task_id: str, timeout_seconds: int = 30) -> dict:
        """Long-poll: block up to ``timeout_seconds`` waiting for new output
        from the task, then return the latest snapshot.

        Use this in a loop to follow the task's progress without busy-waiting.
        The snapshot's ``accumulated_text`` is the *full* output so far; diff
        against your previous call to know what's new.

        Args:
            task_id: Returned by ``start_task``.
            timeout_seconds: Max poll duration. Capped at the server's
                ``WAIT_MAX_TIMEOUT`` (default 120s).
        """
        bridge = _require_bridge()
        task = bridge.pool.get_task(task_id)
        if task is None:
            raise ToolError(f"unknown task_id: {task_id}")

        if task.is_done:
            return task.snapshot()

        timeout = max(1, min(timeout_seconds, _wait_max_timeout))
        baseline_count = len(task.chunks)
        baseline_status = task.status

        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            now = asyncio.get_running_loop().time()
            remaining = deadline - now
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(task.update_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            # Anything new since the call started?
            if (
                len(task.chunks) > baseline_count
                or task.status is not baseline_status
            ):
                break

        return task.snapshot()

    @mcp.tool()
    def get_task_status(task_id: str) -> dict:
        """Non-blocking snapshot of a task. Cheaper than ``wait_for_progress``
        when you only need a quick check.
        """
        bridge = _require_bridge()
        task = bridge.pool.get_task(task_id)
        if task is None:
            raise ToolError(f"unknown task_id: {task_id}")
        return task.snapshot()

    @mcp.tool()
    async def cancel_task(task_id: str, reason: str = "") -> dict:
        """Cancel a running task.

        Soft-cancel only: the bridge stops returning data for this task to
        you. The remote device may still be running until its agent honors
        the cancel frame (current AI_Proxy build only logs the frame).

        Args:
            task_id: Task to abort.
            reason: Optional human-readable reason (recorded in audit logs).
        """
        bridge = _require_bridge()
        task = await bridge.cancel(task_id, reason=reason)
        if task is None:
            raise ToolError(f"unknown task_id: {task_id}")
        return task.snapshot()

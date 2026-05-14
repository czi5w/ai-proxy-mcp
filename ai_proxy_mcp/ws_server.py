"""WebSocket server: accepts reverse connections from AI_Proxy clients.

Wire protocol (sent as JSON text frames):

    AI_Proxy -> Bridge:
        { "type": "register", "device_id": "<id>" }                        (first frame, required)
        { "type": "chunk",    "id": "<task_id>", "content": "..." }
        { "type": "done",     "id": "<task_id>" }
        { "type": "error",    "id": "<task_id>", "message": "..." }

    Bridge -> AI_Proxy:
        { "type": "request",  "id": "<task_id>",
          "messages": [{ "role": "user", "content": "..." }] }
        { "type": "cancel",   "id": "<task_id>" }
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Optional

import websockets

from .config import Config
from .device_pool import DevicePool, Task, is_ws_open

logger = logging.getLogger(__name__)


class Bridge:
    """High-level facade combining the device pool with request/response helpers.

    The MCP tools talk to this class; they should not touch the WebSocket
    objects directly.
    """

    def __init__(self, pool: DevicePool) -> None:
        self.pool = pool

    # ── Public API for tools ───────────────────────────────────

    async def start(self, device_id: str, prompt: str) -> Task:
        """Send a `request` frame to the device and register a Task in the
        ledger. Returns immediately with the Task object (status=RUNNING)."""
        ws = self.pool.get(device_id)
        if ws is None or not is_ws_open(ws):
            raise RuntimeError(f'device "{device_id}" not connected')

        task_id = str(uuid.uuid4())
        task = self.pool.open_task(task_id, device_id, prompt)

        frame = json.dumps(
            {
                "type": "request",
                "id": task_id,
                "messages": [{"role": "user", "content": prompt}],
            }
        )
        try:
            await ws.send(frame)
        except Exception as exc:
            self.pool.mark_done(
                task_id, error=f"failed to send request: {exc}"
            )
            raise RuntimeError(f"failed to send request to {device_id}: {exc}") from exc

        return task

    async def cancel(self, task_id: str, reason: str = "") -> Optional[Task]:
        """Soft-cancel: mark the task CANCELLED locally so subsequent chunks
        are dropped, then send a cancel frame to the device. Whether the device
        actually stops depends on its implementation (currently AI_Proxy only
        logs the cancel frame).
        """
        task = self.pool.get_task(task_id)
        if task is None:
            return None
        if task.is_done:
            return task

        # Mark cancelled before sending so that any chunks racing in get dropped.
        self.pool.mark_done(task_id, cancelled=True, error=reason or None)

        ws = self.pool.get(task.device_id)
        if ws is not None and is_ws_open(ws):
            frame = json.dumps({"type": "cancel", "id": task_id, "reason": reason})
            try:
                await ws.send(frame)
            except Exception:
                logger.exception(
                    'failed to deliver cancel frame to device "%s"', task.device_id
                )
        return task


async def _read_register(ws, register_timeout: int) -> Optional[str]:
    """Wait for the first frame; if it's a valid register, return device_id."""
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=register_timeout)
    except asyncio.TimeoutError:
        await ws.close(code=1008, reason="register timeout")
        return None

    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        await ws.close(code=1002, reason="invalid JSON")
        return None

    if msg.get("type") != "register" or not msg.get("device_id"):
        await ws.close(code=1002, reason="expected register frame")
        return None
    return str(msg["device_id"])


async def _handle_inbound(ws, device_id: str, pool: DevicePool) -> None:
    """Loop reading frames from a registered device until it disconnects."""
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[%s] invalid JSON frame, dropping", device_id)
            continue

        msg_type = msg.get("type")
        task_id = msg.get("id")

        if msg_type == "chunk":
            if not task_id:
                continue
            content = msg.get("content") or ""
            pool.update_chunk(task_id, content)

        elif msg_type == "done":
            if not task_id:
                continue
            pool.mark_done(task_id)

        elif msg_type == "error":
            if not task_id:
                continue
            err = msg.get("message") or "AI_Proxy error"
            pool.mark_done(task_id, error=err)

        else:
            logger.debug("[%s] ignoring frame type=%r", device_id, msg_type)


def make_handler(cfg: Config, pool: DevicePool):
    """Return a websockets connection handler bound to this pool/config."""

    async def handler(ws) -> None:
        peer = ws.remote_address
        logger.info("connection opened from %s, awaiting register", peer)

        device_id = await _read_register(ws, cfg.register_timeout_seconds)
        if device_id is None:
            logger.warning("register failed for %s", peer)
            return

        prev = pool.register(device_id, ws)
        if prev is not None and is_ws_open(prev):
            logger.warning(
                'device "%s" reconnected; closing previous connection', device_id
            )
            await prev.close(code=1000, reason="replaced by new connection")

        logger.info(
            'device "%s" registered from %s (total=%d)',
            device_id,
            peer,
            len(pool.list_devices()),
        )

        try:
            await _handle_inbound(ws, device_id, pool)
        except websockets.ConnectionClosed as exc:
            logger.info('device "%s" closed: %s', device_id, exc)
        except Exception:
            logger.exception('unexpected error handling device "%s"', device_id)
        finally:
            pool.unregister(device_id, ws)
            logger.info(
                'device "%s" unregistered (total=%d)',
                device_id,
                len(pool.list_devices()),
            )

    return handler


async def serve_ws(cfg: Config, pool: DevicePool):
    handler = make_handler(cfg, pool)
    # Heartbeat strategy:
    # - We MUST send something every minute or so, otherwise NAT/firewall
    #   idle timeouts (often 120s) drop the TCP connection silently.
    # - But AI_Proxy C++ is single-threaded inside its ACP loop and may
    #   not read pings while a long Copilot run is in flight, so we set
    #   a very generous ping_timeout to avoid killing healthy long tasks.
    server = await websockets.serve(
        handler,
        cfg.ws_host,
        cfg.ws_port,
        ping_interval=cfg.ws_ping_interval_seconds,
        ping_timeout=cfg.ws_ping_timeout_seconds,
        max_size=8 * 1024 * 1024,
    )
    logger.info(
        "WS server listening on ws://%s:%d (ping_interval=%ss, ping_timeout=%ss)",
        cfg.ws_host,
        cfg.ws_port,
        cfg.ws_ping_interval_seconds,
        cfg.ws_ping_timeout_seconds,
    )
    return server


async def cleanup_loop(pool: DevicePool, interval_seconds: int = 60) -> None:
    """Background task: periodically prune finished, expired entries from the
    task ledger so memory stays bounded."""
    while True:
        await asyncio.sleep(interval_seconds)
        removed = pool.cleanup_old_tasks()
        if removed:
            logger.debug("pruned %d stale tasks", removed)

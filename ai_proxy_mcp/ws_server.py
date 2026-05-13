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
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Optional

import websockets

from .config import Config
from .device_pool import DevicePool, is_ws_open

logger = logging.getLogger(__name__)


class Bridge:
    """High-level facade combining the device pool with request/response helpers.

    The MCP tools talk to this class; they should not touch the WebSocket
    objects directly.
    """

    def __init__(self, pool: DevicePool, task_timeout: int) -> None:
        self.pool = pool
        self.task_timeout = task_timeout

    async def run_on_device(self, device_id: str, prompt: str) -> str:
        """Send `prompt` to the named device, wait for its full reply.

        Raises:
            RuntimeError: device offline, sending failed, AI_Proxy returned error,
                or the device disconnected mid-task.
            asyncio.TimeoutError: no `done` frame within ``task_timeout`` seconds.
        """
        ws = self.pool.get(device_id)
        if ws is None or not is_ws_open(ws):
            raise RuntimeError(f'device "{device_id}" not connected')

        task_id = str(uuid.uuid4())
        task = self.pool.open_task(task_id, device_id)

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
            self.pool.close_task(task_id)
            raise RuntimeError(f"failed to send request to {device_id}: {exc}") from exc

        try:
            return await asyncio.wait_for(task.future, timeout=self.task_timeout)
        except asyncio.TimeoutError:
            self.pool.close_task(task_id)
            raise


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
            task = pool.get_task(task_id) if task_id else None
            if task and task.device_id == device_id:
                task.accumulated.append(msg.get("content") or "")
            else:
                logger.debug("[%s] orphan chunk for task %s", device_id, task_id)

        elif msg_type == "done":
            task = pool.close_task(task_id) if task_id else None
            if task and not task.future.done():
                task.future.set_result("".join(task.accumulated))
            else:
                logger.debug("[%s] orphan done for task %s", device_id, task_id)

        elif msg_type == "error":
            task = pool.close_task(task_id) if task_id else None
            err = msg.get("message") or "AI_Proxy error"
            if task and not task.future.done():
                task.future.set_exception(RuntimeError(err))
            else:
                logger.warning("[%s] orphan error: %s", device_id, err)

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


async def serve_ws(cfg: Config, pool: DevicePool) -> websockets.WebSocketServer:
    handler = make_handler(cfg, pool)
    server = await websockets.serve(handler, cfg.ws_host, cfg.ws_port)
    logger.info("WS server listening on ws://%s:%d", cfg.ws_host, cfg.ws_port)
    return server

"""Device connection pool and pending-task ledger.

Maintains the set of currently-connected AI_Proxy clients (keyed by
``device_id``) and the in-flight RPC requests that are waiting for a
``done`` / ``error`` frame from a specific device.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


def is_ws_open(ws: Any) -> bool:
    """Cross-version check for whether a websocket is open.

    websockets >= 14 exposes ``state`` (a State enum) on the new asyncio
    API (``ServerConnection``) and dropped ``closed``. The legacy
    ``WebSocketServerProtocol`` (< 14) still has ``closed``. Support both.
    """
    state = getattr(ws, "state", None)
    if state is not None:
        return getattr(state, "name", "") == "OPEN"
    return not getattr(ws, "closed", True)


@dataclass
class PendingTask:
    """A request that has been sent to a device and is awaiting the final frame."""

    device_id: str
    accumulated: list[str] = field(default_factory=list)
    future: asyncio.Future[str] = field(
        default_factory=lambda: asyncio.get_event_loop().create_future()
    )
    created_ts: float = field(default_factory=lambda: asyncio.get_event_loop().time())


class DevicePool:
    """Thread-safe (single-loop) registry of device connections and pending tasks."""

    def __init__(self) -> None:
        self._devices: dict[str, Any] = {}
        self._pending: dict[str, PendingTask] = {}

    # ── Device lifecycle ────────────────────────────────────────

    def register(self, device_id: str, ws: Any) -> Optional[Any]:
        """Register a new connection for the given device. Returns the previous
        connection (if any) so the caller can close it."""
        prev = self._devices.get(device_id)
        self._devices[device_id] = ws
        return prev

    def unregister(self, device_id: str, ws: Any) -> None:
        """Remove the device only if the active connection still matches `ws`."""
        if self._devices.get(device_id) is ws:
            del self._devices[device_id]
        # Reject any pending tasks tied to this device.
        for task_id, task in list(self._pending.items()):
            if task.device_id == device_id and not task.future.done():
                task.future.set_exception(
                    RuntimeError(f'device "{device_id}" disconnected mid-task')
                )
                self._pending.pop(task_id, None)

    def list_devices(self) -> list[str]:
        return sorted(self._devices.keys())

    def is_connected(self, device_id: str) -> bool:
        ws = self._devices.get(device_id)
        return ws is not None and is_ws_open(ws)

    def get(self, device_id: str) -> Optional[Any]:
        return self._devices.get(device_id)

    def busy_devices(self) -> set[str]:
        """Devices with at least one in-flight task."""
        return {task.device_id for task in self._pending.values() if not task.future.done()}

    # ── Pending tasks ───────────────────────────────────────────

    def open_task(self, task_id: str, device_id: str) -> PendingTask:
        if task_id in self._pending:
            raise ValueError(f"task_id {task_id} already pending")
        task = PendingTask(device_id=device_id)
        self._pending[task_id] = task
        return task

    def close_task(self, task_id: str) -> Optional[PendingTask]:
        return self._pending.pop(task_id, None)

    def get_task(self, task_id: str) -> Optional[PendingTask]:
        return self._pending.get(task_id)

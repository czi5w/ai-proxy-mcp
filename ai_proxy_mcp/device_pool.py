"""Device connection pool and observable task ledger.

Maintains:
- The set of currently-connected AI_Proxy clients (keyed by ``device_id``).
- The task ledger: each task has a status machine (running/done/failed/
  cancelled), accumulated chunks, an asyncio.Event that fires whenever new
  data arrives so that ``wait_for_progress`` can long-poll cheaply.

Tasks are kept in the ledger for ``retention_seconds`` after they finish so
the LLM can still query their final state. Older finished tasks get pruned.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
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


class TaskStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Task:
    """One in-flight or recently-finished request to a device."""

    task_id: str
    device_id: str
    prompt: str
    status: TaskStatus = TaskStatus.RUNNING
    chunks: list[str] = field(default_factory=list)
    error: Optional[str] = None
    started_ts: float = field(default_factory=time.time)
    done_ts: Optional[float] = None
    # Fires whenever chunks/status changes. Long-pollers (``wait_for_progress``)
    # await it. We re-create the event after each notify_all so a single waiter
    # never gets stuck holding a stale set state.
    update_event: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def accumulated(self) -> str:
        return "".join(self.chunks)

    @property
    def is_done(self) -> bool:
        return self.status is not TaskStatus.RUNNING

    def snapshot(self) -> dict:
        now = time.time()
        elapsed = (self.done_ts or now) - self.started_ts
        return {
            "task_id": self.task_id,
            "device_id": self.device_id,
            "status": self.status.value,
            "elapsed_seconds": round(elapsed, 2),
            "chunks_count": len(self.chunks),
            "accumulated_text": self.accumulated,
            "is_done": self.is_done,
            "error": self.error,
        }


class DevicePool:
    """Single-event-loop registry of device connections and tasks."""

    def __init__(self, retention_seconds: int = 600) -> None:
        self._devices: dict[str, Any] = {}
        self._tasks: dict[str, Task] = {}
        self._retention_seconds = retention_seconds

    # ── Device lifecycle ────────────────────────────────────────

    def register(self, device_id: str, ws: Any) -> Optional[Any]:
        """Register a new connection. Returns previous connection (if any) so
        the caller can close it."""
        prev = self._devices.get(device_id)
        self._devices[device_id] = ws
        return prev

    def unregister(self, device_id: str, ws: Any) -> None:
        """Remove the device only if the active connection still matches `ws`.
        Mark all of that device's still-running tasks as failed."""
        if self._devices.get(device_id) is ws:
            del self._devices[device_id]
        for task in self._tasks.values():
            if task.device_id == device_id and not task.is_done:
                self._set_done(
                    task,
                    TaskStatus.FAILED,
                    error=f'device "{device_id}" disconnected mid-task',
                )

    def list_devices(self) -> list[str]:
        return sorted(self._devices.keys())

    def is_connected(self, device_id: str) -> bool:
        ws = self._devices.get(device_id)
        return ws is not None and is_ws_open(ws)

    def get(self, device_id: str) -> Optional[Any]:
        return self._devices.get(device_id)

    def busy_devices(self) -> set[str]:
        """Devices with at least one running task."""
        return {t.device_id for t in self._tasks.values() if not t.is_done}

    # ── Task lifecycle ──────────────────────────────────────────

    def open_task(self, task_id: str, device_id: str, prompt: str) -> Task:
        if task_id in self._tasks:
            raise ValueError(f"task_id {task_id} already exists")
        task = Task(task_id=task_id, device_id=device_id, prompt=prompt)
        self._tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[Task]:
        return list(self._tasks.values())

    def update_chunk(self, task_id: str, content: str) -> None:
        task = self._tasks.get(task_id)
        if task is None or task.is_done:
            return
        task.chunks.append(content)
        self._notify(task)

    def mark_done(
        self,
        task_id: str,
        *,
        error: Optional[str] = None,
        cancelled: bool = False,
    ) -> Optional[Task]:
        task = self._tasks.get(task_id)
        if task is None or task.is_done:
            return task
        if cancelled:
            status = TaskStatus.CANCELLED
        elif error:
            status = TaskStatus.FAILED
        else:
            status = TaskStatus.DONE
        self._set_done(task, status, error=error)
        return task

    def cleanup_old_tasks(self) -> int:
        """Remove tasks that finished more than ``retention_seconds`` ago.
        Returns the number removed."""
        cutoff = time.time() - self._retention_seconds
        stale = [
            tid
            for tid, t in self._tasks.items()
            if t.is_done and t.done_ts is not None and t.done_ts < cutoff
        ]
        for tid in stale:
            del self._tasks[tid]
        return len(stale)

    # ── Internals ───────────────────────────────────────────────

    def _set_done(
        self, task: Task, status: TaskStatus, *, error: Optional[str] = None
    ) -> None:
        task.status = status
        task.done_ts = time.time()
        if error is not None:
            task.error = error
        self._notify(task)

    @staticmethod
    def _notify(task: Task) -> None:
        """Wake every waiter on update_event. Set-then-clear in the same tick
        is the canonical pattern: any task currently parked in
        ``await event.wait()`` is already past the suspend point and will be
        scheduled, while future ``wait()`` calls see ``is_set()==False`` and
        block as expected.
        """
        task.update_event.set()
        task.update_event.clear()

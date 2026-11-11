"""Task Registry for monitoring and safely awaiting in-flight execution tasks during shutdown."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class TaskRegistry:
    """Tracks active in-flight trade entries and closes to guarantee safe shutdown."""

    def __init__(self) -> None:
        self._tasks: Dict[str, asyncio.Task] = {}
        self._lock = asyncio.Lock()
        self._accepting_entries: bool = True

    @property
    def accepting_entries(self) -> bool:
        return self._accepting_entries

    def stop_accepting_entries(self) -> None:
        self._accepting_entries = False

    async def register(self, task_id: str, task: asyncio.Task) -> None:
        async with self._lock:
            self._tasks[task_id] = task

    async def unregister(self, task_id: str) -> None:
        async with self._lock:
            self._tasks.pop(task_id, None)

    async def wait_for_completion(self, timeout_sec: float = 45.0) -> bool:
        """
        Wait for all registered tasks to complete cleanly.
        Returns True if all completed, False if timed out.
        """
        self.stop_accepting_entries()
        start = time.time()
        while time.time() - start < timeout_sec:
            async with self._lock:
                active = [t for t in self._tasks.values() if not t.done()]
            if not active:
                return True
            await asyncio.sleep(0.2)

        async with self._lock:
            remaining = len([t for t in self._tasks.values() if not t.done()])
        logger.warning("TaskRegistry wait timed out with %d tasks still in-flight.", remaining)
        return False

from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)


class CleanupScheduler:
    def __init__(self, interval_seconds: int = 30) -> None:
        self.interval_seconds = interval_seconds
        self._entries: list[tuple[float, Path]] = []
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="cleanup-scheduler")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def schedule(self, path: Path, delay_seconds: int) -> None:
        deadline = time.monotonic() + delay_seconds
        async with self._lock:
            self._entries.append((deadline, path))

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            now = time.monotonic()
            due: list[Path] = []
            pending: list[tuple[float, Path]] = []

            async with self._lock:
                for deadline, path in self._entries:
                    if deadline <= now:
                        due.append(path)
                    else:
                        pending.append((deadline, path))
                self._entries = pending

            for path in due:
                await asyncio.to_thread(self._delete_path_sync, path)

    def _delete_path_sync(self, path: Path) -> None:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink(missing_ok=True)
        except Exception:
            logger.exception("Failed to delete scheduled path: %s", path)


from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, max_actions: int, window_seconds: int) -> None:
        self.max_actions = max_actions
        self.window_seconds = window_seconds
        self._history: dict[int, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def consume(self, user_id: int) -> tuple[bool, int, int]:
        now = time.monotonic()
        async with self._lock:
            history = self._history[user_id]
            cutoff = now - self.window_seconds
            while history and history[0] < cutoff:
                history.popleft()

            if len(history) >= self.max_actions:
                retry_after = int(self.window_seconds - (now - history[0]))
                return False, 0, max(retry_after, 1)

            history.append(now)
            remaining = self.max_actions - len(history)
            return True, remaining, 0


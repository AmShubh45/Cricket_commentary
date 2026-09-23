"""In-memory match state store — drop-in replacement for Redis.

Used when Redis is not available (local development, testing).
Provides the same interface as MatchStateStore but stores
everything in Python dicts. Not suitable for production
(no persistence, no multi-process sharing).
"""

from __future__ import annotations

import logging
from typing import Any

from cricket.domain.models import MatchState

logger = logging.getLogger(__name__)


class InMemoryStateStore:
    """In-memory match state store implementing the same interface as MatchStateStore.

    Usage:
        store = InMemoryStateStore()
        await store.save_state("match_123", match_state)
        state = await store.get_state("match_123")
    """

    def __init__(self) -> None:
        self._states: dict[str, MatchState] = {}
        self._last_balls: dict[str, str] = {}
        self._history: dict[str, list[dict]] = {}
        logger.info("Using in-memory state store (no Redis)")

    async def save_state(self, match_id: str, state: MatchState) -> None:
        self._states[match_id] = state

    async def get_state(self, match_id: str) -> MatchState | None:
        return self._states.get(match_id)

    async def set_last_processed_ball(self, match_id: str, ball_key: str) -> None:
        self._last_balls[match_id] = ball_key

    async def get_last_processed_ball(self, match_id: str) -> str | None:
        return self._last_balls.get(match_id)

    async def append_to_history(
        self,
        match_id: str,
        entry: dict[str, Any],
        max_entries: int = 300,
    ) -> None:
        if match_id not in self._history:
            self._history[match_id] = []
        self._history[match_id].append(entry)
        self._history[match_id] = self._history[match_id][-max_entries:]

    async def get_recent_history(self, match_id: str, count: int = 10) -> list[dict]:
        return self._history.get(match_id, [])[-count:]

    async def delete_match_data(self, match_id: str) -> None:
        self._states.pop(match_id, None)
        self._last_balls.pop(match_id, None)
        self._history.pop(match_id, None)

    async def healthcheck(self) -> bool:
        return True

    async def close(self) -> None:
        pass

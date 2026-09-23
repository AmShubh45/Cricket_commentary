"""Redis client for match state management.

Handles:
- Live match state (MatchState) storage and retrieval
- Last processed ball tracking (for delta detection)
- Match event history (for context-aware commentary)

Uses Redis as a fast, ephemeral state store for live match data.
PostgreSQL handles persistent logging; Redis handles real-time state.
"""

from __future__ import annotations

import logging
from typing import Any

import orjson
import redis.asyncio as redis

from config.settings import RedisSettings
from cricket.domain.models import MatchState

logger = logging.getLogger(__name__)


class MatchStateStore:
    """Redis-backed match state store for live match data.

    Usage:
        store = MatchStateStore(redis_settings)
        await store.save_state("match_123", match_state)
        state = await store.get_state("match_123")
    """

    # Key prefixes for organized namespace
    _STATE_PREFIX = "match:state:"
    _LAST_BALL_PREFIX = "match:last_ball:"
    _HISTORY_PREFIX = "match:history:"

    def __init__(self, settings: RedisSettings) -> None:
        self._client = redis.from_url(
            settings.url,
            db=settings.match_state_db,
            decode_responses=False,  # We handle decoding ourselves with orjson
        )

    async def save_state(self, match_id: str, state: MatchState) -> None:
        """Save the current match state to Redis.

        State is serialized with orjson for speed and stored with a
        24-hour TTL (matches don't last longer than that).
        """
        key = f"{self._STATE_PREFIX}{match_id}"
        data = orjson.dumps(state.model_dump(mode="json"))
        await self._client.set(key, data, ex=86400)  # 24h TTL

    async def get_state(self, match_id: str) -> MatchState | None:
        """Retrieve the current match state from Redis."""
        key = f"{self._STATE_PREFIX}{match_id}"
        data = await self._client.get(key)
        if data is None:
            return None

        try:
            parsed = orjson.loads(data)
            return MatchState.model_validate(parsed)
        except Exception:
            logger.exception("Failed to deserialize match state for %s", match_id)
            return None

    async def set_last_processed_ball(self, match_id: str, ball_key: str) -> None:
        """Store the key of the last processed ball for delta detection.

        ball_key format: "{innings}_{over}_{ball}" e.g., "1_4_3"
        """
        key = f"{self._LAST_BALL_PREFIX}{match_id}"
        await self._client.set(key, ball_key.encode(), ex=86400)

    async def get_last_processed_ball(self, match_id: str) -> str | None:
        """Get the key of the last processed ball."""
        key = f"{self._LAST_BALL_PREFIX}{match_id}"
        data = await self._client.get(key)
        if data is None:
            return None
        return data.decode() if isinstance(data, bytes) else data

    async def append_to_history(
        self,
        match_id: str,
        entry: dict[str, Any],
        max_entries: int = 300,
    ) -> None:
        """Append a commentary/event entry to the match history list.

        Keeps the last `max_entries` entries for context-aware commentary.
        """
        key = f"{self._HISTORY_PREFIX}{match_id}"
        data = orjson.dumps(entry)
        pipe = self._client.pipeline()
        pipe.rpush(key, data)
        pipe.ltrim(key, -max_entries, -1)  # Keep only last N entries
        pipe.expire(key, 86400)
        await pipe.execute()

    async def get_recent_history(
        self,
        match_id: str,
        count: int = 10,
    ) -> list[dict]:
        """Get the last N commentary entries for context."""
        key = f"{self._HISTORY_PREFIX}{match_id}"
        entries = await self._client.lrange(key, -count, -1)
        return [orjson.loads(e) for e in entries]

    async def delete_match_data(self, match_id: str) -> None:
        """Clean up all Redis keys for a completed match."""
        keys = [
            f"{self._STATE_PREFIX}{match_id}",
            f"{self._LAST_BALL_PREFIX}{match_id}",
            f"{self._HISTORY_PREFIX}{match_id}",
        ]
        await self._client.delete(*keys)
        logger.info("Cleaned up Redis data for match %s", match_id)

    async def healthcheck(self) -> bool:
        """Verify Redis connectivity."""
        try:
            await self._client.ping()
            return True
        except Exception:
            logger.exception("Redis healthcheck failed")
            return False

    async def close(self) -> None:
        """Close the Redis connection."""
        await self._client.aclose()

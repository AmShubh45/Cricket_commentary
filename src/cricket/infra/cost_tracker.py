"""Cost tracker — monitors API spend and alerts on budget overruns.

Logs every API call cost to PostgreSQL (when available) and Redis
(for real-time dashboards). Provides per-match and monthly aggregation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import orjson
import redis.asyncio as redis

from config.settings import CostTrackingSettings, RedisSettings
from cricket.domain.models import APICallCost

logger = logging.getLogger(__name__)

# USD to INR approximate rate (for budget alerts in INR)
_USD_TO_INR = 84.0


class CostTracker:
    """Tracks API costs across all providers.

    Stores costs in Redis for real-time access and provides
    aggregation methods for dashboards and budget alerts.

    Usage:
        tracker = CostTracker(cost_settings, redis_settings)
        await tracker.record(cost_entry)
        monthly = await tracker.get_monthly_total()
    """

    _COST_PREFIX = "cost:log:"
    _MONTHLY_KEY = "cost:monthly:"
    _MATCH_KEY = "cost:match:"

    def __init__(
        self,
        cost_settings: CostTrackingSettings,
        redis_settings: RedisSettings,
    ) -> None:
        self._enabled = cost_settings.enabled
        self._budget_alert_inr = cost_settings.monthly_budget_alert_inr
        self._client = redis.from_url(
            redis_settings.url,
            db=0,
            decode_responses=False,
        )

    async def record(self, cost: APICallCost) -> None:
        """Record an API call cost.

        Updates both per-match and monthly aggregates.
        """
        if not self._enabled:
            return

        month_key = f"{self._MONTHLY_KEY}{cost.timestamp.strftime('%Y-%m')}"
        match_key = f"{self._MATCH_KEY}{cost.match_id}" if cost.match_id else None

        pipe = self._client.pipeline()

        # Increment monthly total
        pipe.incrbyfloat(month_key, cost.cost_usd)
        pipe.expire(month_key, 86400 * 35)  # Keep for ~1 month

        # Increment per-match total
        if match_key:
            pipe.incrbyfloat(match_key, cost.cost_usd)
            pipe.expire(match_key, 86400 * 7)  # Keep for 1 week

        # Log individual entry (capped list for recent history)
        log_key = f"{self._COST_PREFIX}{cost.timestamp.strftime('%Y-%m-%d')}"
        entry = orjson.dumps({
            "provider": cost.provider,
            "operation": cost.operation,
            "cost_usd": cost.cost_usd,
            "match_id": cost.match_id,
            "tokens_in": cost.tokens_in,
            "tokens_out": cost.tokens_out,
            "characters": cost.characters,
            "timestamp": cost.timestamp.isoformat(),
        })
        pipe.rpush(log_key, entry)
        pipe.ltrim(log_key, -1000, -1)  # Keep last 1000 entries per day
        pipe.expire(log_key, 86400 * 7)

        await pipe.execute()

        # Check budget alert
        await self._check_budget_alert(month_key)

    async def get_monthly_total_usd(self) -> float:
        """Get the current month's total spend in USD."""
        month_key = f"{self._MONTHLY_KEY}{datetime.utcnow().strftime('%Y-%m')}"
        total = await self._client.get(month_key)
        return float(total) if total else 0.0

    async def get_monthly_total_inr(self) -> float:
        """Get the current month's total spend in INR."""
        usd = await self.get_monthly_total_usd()
        return round(usd * _USD_TO_INR, 2)

    async def get_match_total_usd(self, match_id: str) -> float:
        """Get the total spend for a specific match."""
        match_key = f"{self._MATCH_KEY}{match_id}"
        total = await self._client.get(match_key)
        return float(total) if total else 0.0

    async def get_recent_costs(self, count: int = 20) -> list[dict]:
        """Get the most recent cost entries."""
        today = datetime.utcnow().strftime("%Y-%m-%d")
        log_key = f"{self._COST_PREFIX}{today}"
        entries = await self._client.lrange(log_key, -count, -1)
        return [orjson.loads(e) for e in entries]

    async def get_breakdown_by_provider(self) -> dict[str, float]:
        """Get cost breakdown by provider for the current month."""
        recent = await self.get_recent_costs(count=1000)
        breakdown: dict[str, float] = {}
        for entry in recent:
            provider = entry.get("provider", "unknown")
            cost = entry.get("cost_usd", 0.0)
            breakdown[provider] = breakdown.get(provider, 0.0) + cost
        return breakdown

    async def _check_budget_alert(self, month_key: str) -> None:
        """Check if monthly spend exceeds the INR budget and log a warning."""
        total_bytes = await self._client.get(month_key)
        if not total_bytes:
            return

        total_usd = float(total_bytes)
        total_inr = total_usd * _USD_TO_INR

        if total_inr >= self._budget_alert_inr:
            logger.warning(
                "🚨 BUDGET ALERT: Monthly spend ₹%.2f exceeds budget ₹%.2f (USD $%.2f)",
                total_inr,
                self._budget_alert_inr,
                total_usd,
            )

        elif total_inr >= self._budget_alert_inr * 0.8:
            logger.info(
                "⚠️ Budget warning: Monthly spend ₹%.2f (80%% of ₹%.2f budget)",
                total_inr,
                self._budget_alert_inr,
            )

    async def close(self) -> None:
        """Close Redis connection."""
        await self._client.aclose()

"""Cost dashboard routes — track spending and budget."""

from __future__ import annotations

from fastapi import APIRouter

from cricket.infra.factory import ProviderFactory

router = APIRouter()


@router.get("/costs/summary")
async def cost_summary() -> dict:
    """Get the current month's cost summary."""
    factory = ProviderFactory()
    tracker = factory.create_cost_tracker()

    try:
        monthly_usd = await tracker.get_monthly_total_usd()
        monthly_inr = await tracker.get_monthly_total_inr()
        breakdown = await tracker.get_breakdown_by_provider()
        recent = await tracker.get_recent_costs(count=10)

        return {
            "monthly_total_usd": round(monthly_usd, 6),
            "monthly_total_inr": round(monthly_inr, 2),
            "budget_inr": 1000.0,
            "budget_used_pct": round((monthly_inr / 1000.0) * 100, 1),
            "breakdown_by_provider": {
                k: round(v, 6) for k, v in breakdown.items()
            },
            "recent_costs": recent[:10],
        }
    except Exception as e:
        return {
            "error": str(e),
            "monthly_total_usd": 0,
            "monthly_total_inr": 0,
            "message": "Cost tracking unavailable (Redis may be down)",
        }
    finally:
        await tracker.close()


@router.get("/costs/match/{match_id}")
async def match_cost(match_id: str) -> dict:
    """Get the total cost for a specific match."""
    factory = ProviderFactory()
    tracker = factory.create_cost_tracker()

    try:
        total_usd = await tracker.get_match_total_usd(match_id)
        return {
            "match_id": match_id,
            "total_usd": round(total_usd, 6),
            "total_inr": round(total_usd * 84, 2),
        }
    finally:
        await tracker.close()

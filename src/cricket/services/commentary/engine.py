"""Commentary Orchestrator — routes events to templates or LLM.

This is the central coordinator for commentary generation. It receives
ClassifiedEvents from the EventClassifier and routes them to either:
- TemplateEngine (for ROUTINE/MINOR events — 85-90% of balls)
- LLM via BedrockProvider (for MAJOR events — boundaries, wickets, milestones)

It also handles:
- Building LLM prompts with full match context
- Langfuse tracing for all LLM calls
- Cost tracking per commentary line
- Fallback from LLM → template if LLM fails or exceeds budget
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from cricket.domain.enums import EmotionTag, EventTier, EventType
from cricket.domain.models import (
    ClassifiedEvent,
    CommentaryLine,
    MatchState,
)
from cricket.providers.base import LLMProviderProtocol
from cricket.services.commentary.template_engine import TemplateEngine

logger = logging.getLogger(__name__)


class CommentaryOrchestrator:
    """Routes classified events to the appropriate commentary generator.

    Usage:
        orchestrator = CommentaryOrchestrator(
            template_engine=TemplateEngine(),
            llm_provider=bedrock_provider,
        )
        commentary = await orchestrator.generate(classified_event, match_state)
    """

    def __init__(
        self,
        template_engine: TemplateEngine,
        llm_provider: LLMProviderProtocol | None = None,
        monthly_llm_budget_usd: float = 5.0,
    ) -> None:
        self._templates = template_engine
        self._llm = llm_provider
        self._monthly_budget = monthly_llm_budget_usd
        self._month_spend_usd = 0.0
        self._month_start = datetime.utcnow().replace(day=1)

    async def generate(
        self,
        event: ClassifiedEvent,
        match_state: MatchState,
    ) -> CommentaryLine:
        """Generate commentary for a classified event.

        Routing logic:
        1. MAJOR events → LLM (if available and within budget) → fallback to template
        2. MINOR events → Template with context variation
        3. ROUTINE events → Simple template
        4. AMBIENT events → Template
        """
        if event.tier == EventTier.MAJOR and self._should_use_llm():
            try:
                return await self._generate_llm_commentary(event, match_state)
            except Exception:
                logger.warning(
                    "LLM commentary failed for %s, falling back to template",
                    event.event_type,
                    exc_info=True,
                )

        return self._generate_template_commentary(event)

    async def _generate_llm_commentary(
        self,
        event: ClassifiedEvent,
        match_state: MatchState,
    ) -> CommentaryLine:
        """Generate commentary using the LLM provider."""
        if self._llm is None:
            raise RuntimeError("LLM provider not configured")

        prompt = self._build_prompt(event)
        context = self._build_context(event, match_state)

        commentary = await self._llm.generate_commentary(
            prompt=prompt,
            match_context=context,
        )

        # Track costs
        self._month_spend_usd += commentary.llm_cost_usd

        logger.info(
            "LLM commentary: %s | cost=$%.6f | total_month=$%.4f",
            commentary.text[:60],
            commentary.llm_cost_usd,
            self._month_spend_usd,
        )

        return commentary

    def _generate_template_commentary(self, event: ClassifiedEvent) -> CommentaryLine:
        """Generate commentary using the template engine."""
        return self._templates.generate(event)

    def _should_use_llm(self) -> bool:
        """Check if we should use the LLM or fall back to templates.

        Conditions for using LLM:
        1. LLM provider is configured
        2. Monthly budget not exceeded
        3. Current month hasn't rolled over (reset counter on new month)
        """
        if self._llm is None:
            return False

        # Reset monthly counter if new month
        now = datetime.utcnow()
        current_month_start = now.replace(day=1)
        if current_month_start > self._month_start:
            self._month_spend_usd = 0.0
            self._month_start = current_month_start

        if self._month_spend_usd >= self._monthly_budget:
            logger.warning(
                "Monthly LLM budget exceeded ($%.2f / $%.2f). Using templates.",
                self._month_spend_usd,
                self._monthly_budget,
            )
            return False

        return True

    @staticmethod
    def _build_prompt(event: ClassifiedEvent) -> str:
        """Build a descriptive prompt for the LLM based on event type."""
        ball = event.ball_event
        prompts: dict[EventType, str] = {
            EventType.BOUNDARY: (
                f"{ball.batsman.display_name} ने {ball.bowler.display_name} की गेंद पर चौका मारा। "
                f"ओवर {ball.over_display}। स्कोर {event.team_score}/{event.team_wickets}।"
            ),
            EventType.SIX: (
                f"{ball.batsman.display_name} ने {ball.bowler.display_name} की गेंद पर छक्का मारा! "
                f"ओवर {ball.over_display}। स्कोर {event.team_score}/{event.team_wickets}।"
            ),
            EventType.WICKET: (
                f"{ball.batsman.display_name} OUT! {ball.bowler.display_name} की गेंद पर विकेट गिरा। "
                f"{ball.batsman.display_name} ने {event.batsman_runs}({event.batsman_balls}) की पारी खेली। "
                f"स्कोर {event.team_score}/{event.team_wickets}।"
            ),
            EventType.MILESTONE_50: (
                f"{ball.batsman.display_name} का अर्धशतक! "
                f"{event.batsman_runs} रन {event.batsman_balls} गेंदों में। "
                f"टीम स्कोर {event.team_score}/{event.team_wickets}।"
            ),
            EventType.MILESTONE_100: (
                f"{ball.batsman.display_name} का शतक! "
                f"{event.batsman_runs} रन {event.batsman_balls} गेंदों में। "
                f"टीम स्कोर {event.team_score}/{event.team_wickets}। ऐतिहासिक पारी!"
            ),
            EventType.MAIDEN_OVER: (
                f"{ball.bowler.display_name} ने मेडन ओवर डाला! "
                f"शानदार गेंदबाज़ी! स्कोर {event.team_score}/{event.team_wickets}।"
            ),
            EventType.FIVE_WICKET_HAUL: (
                f"{ball.bowler.display_name} को 5 विकेट! "
                f"गज़ब की गेंदबाज़ी! स्कोर {event.team_score}/{event.team_wickets}।"
            ),
        }

        return prompts.get(
            event.event_type,
            f"{ball.batsman.display_name} ने {ball.bowler.display_name} की गेंद पर "
            f"{ball.runs_total} रन लिए। स्कोर {event.team_score}/{event.team_wickets}।",
        )

    @staticmethod
    def _build_context(event: ClassifiedEvent, state: MatchState) -> dict[str, Any]:
        """Build match context dict for the LLM."""
        innings = state.get_current_innings()
        context: dict[str, Any] = {
            "match_id": event.ball_event.match_id,
            "match_title": state.match_info.title,
            "format": state.match_info.format.value,
            "innings": event.ball_event.innings,
            "over_display": event.ball_event.over_display,
            "event_type": event.event_type.value,
            "emotion": event.emotion.value,
            "batsman": event.ball_event.batsman.display_name,
            "bowler": event.ball_event.bowler.display_name,
            "batsman_score": f"{event.batsman_runs}({event.batsman_balls})",
            "team_score": f"{event.team_score}/{event.team_wickets}",
            "phase": event.phase.value,
            "situation": event.situation.value,
            "partnership_runs": event.partnership_runs,
        }

        if innings and innings.target:
            context["target"] = innings.target
            context["runs_needed"] = innings.target - innings.total_runs
            context["required_rate"] = innings.required_rate or 0

        return context

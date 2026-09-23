"""Event Classifier — routes ball events to template or LLM commentary.

This is the decision engine that determines:
1. WHAT type of event occurred (boundary, wicket, dot ball, milestone, etc.)
2. HOW important it is (EventTier: MAJOR → LLM, MINOR/ROUTINE → templates)
3. WHAT emotional tone the commentary should carry
4. WHAT match context/phase the event occurred in

Design: Pure functions + stateless class — no side effects, easily testable.
The classifier needs MatchState to detect milestones, phase, and situation.
"""

from __future__ import annotations

import logging

from cricket.domain.enums import (
    EmotionTag,
    EventTier,
    EventType,
    InningsPhase,
    MatchFormat,
    MatchSituation,
)
from cricket.domain.models import BallEvent, ClassifiedEvent, MatchState

logger = logging.getLogger(__name__)


class EventClassifier:
    """Classifies ball events into types, tiers, and emotional context.

    Usage:
        classifier = EventClassifier()
        classified = classifier.classify(ball_event, match_state)
        # classified.tier → EventTier.MAJOR  (send to LLM)
        # classified.emotion → EmotionTag.EXCITED
    """

    def classify(self, event: BallEvent, state: MatchState) -> ClassifiedEvent:
        """Classify a ball event with full match context.

        This is the main entry point. It chains together:
        1. Event type detection (what happened)
        2. Milestone detection (50s, 100s, partnerships)
        3. Tier assignment (MAJOR/MINOR/ROUTINE)
        4. Phase detection (powerplay/middle/death)
        5. Situation detection (chasing/defending/comfortable/tight)
        6. Emotion assignment
        """
        event_type = self._detect_event_type(event)
        event_type = self._check_milestones(event, state, event_type)
        tier = self._assign_tier(event, event_type, state)
        phase = self._detect_phase(event, state)
        situation = self._detect_situation(state)
        emotion = self._assign_emotion(event_type, tier, situation)

        # Extract current batsman/team stats from match state
        innings = state.get_current_innings()
        batsman_runs, batsman_balls = 0, 0
        team_score, team_wickets, team_overs = 0, 0, 0.0
        target, runs_needed, balls_remaining = None, None, None
        partnership_runs = 0

        if innings:
            team_score = innings.total_runs
            team_wickets = innings.total_wickets
            team_overs = innings.total_overs
            partnership_runs = innings.partnership_runs

            for batter in innings.batsmen:
                if batter.player.name == event.batsman.name:
                    batsman_runs = batter.runs
                    batsman_balls = batter.balls_faced
                    break

            if innings.target is not None:
                target = innings.target
                runs_needed = innings.target - innings.total_runs
                # Calculate balls remaining based on format
                total_overs = self._max_overs(state.match_info.format)
                overs_bowled = innings.total_overs
                balls_remaining = int((total_overs - overs_bowled) * 6)

        return ClassifiedEvent(
            ball_event=event,
            event_type=event_type,
            tier=tier,
            phase=phase,
            situation=situation,
            emotion=emotion,
            batsman_runs=batsman_runs,
            batsman_balls=batsman_balls,
            team_score=team_score,
            team_wickets=team_wickets,
            team_overs=team_overs,
            target=target,
            runs_needed=runs_needed,
            balls_remaining=balls_remaining,
            partnership_runs=partnership_runs,
        )

    # =========================================================================
    # Event Type Detection
    # =========================================================================

    @staticmethod
    def _detect_event_type(event: BallEvent) -> EventType:
        """Determine what kind of delivery this was."""
        # Wicket takes priority
        if event.wicket:
            return EventType.WICKET

        # Boundaries
        if event.is_six:
            return EventType.SIX
        if event.is_boundary:
            return EventType.BOUNDARY

        # Extras
        if event.is_wide:
            return EventType.WIDE
        if event.is_no_ball:
            return EventType.NO_BALL
        if event.is_bye:
            return EventType.BYE
        if event.is_leg_bye:
            return EventType.LEG_BYE

        # Runs
        if event.runs_batsman == 0:
            return EventType.DOT_BALL
        if event.runs_batsman == 1:
            return EventType.SINGLE
        if event.runs_batsman == 2:
            return EventType.DOUBLE
        if event.runs_batsman == 3:
            return EventType.TRIPLE

        return EventType.DOT_BALL

    @staticmethod
    def _check_milestones(
        event: BallEvent,
        state: MatchState,
        current_type: EventType,
    ) -> EventType:
        """Check if this ball triggers a milestone (50, 100, etc.).

        Milestones override the base event type because they're more
        commentary-worthy than the underlying shot.
        """
        innings = state.get_current_innings()
        if not innings:
            return current_type

        # Find the current batsman's score BEFORE this ball
        for batter in innings.batsmen:
            if batter.player.name == event.batsman.name:
                score_before = batter.runs
                score_after = score_before + event.runs_batsman

                # Check milestone crossings
                if score_before < 50 <= score_after:
                    return EventType.MILESTONE_50
                if score_before < 100 <= score_after:
                    return EventType.MILESTONE_100
                if score_before < 150 <= score_after:
                    return EventType.MILESTONE_150
                if score_before < 200 <= score_after:
                    return EventType.MILESTONE_200
                break

        # Check partnership milestones
        partnership = innings.partnership_runs + event.runs_total
        if innings.partnership_runs < 50 <= partnership:
            return EventType.PARTNERSHIP_50
        if innings.partnership_runs < 100 <= partnership:
            return EventType.PARTNERSHIP_100

        # Check bowler milestones (5-wicket haul)
        if event.wicket and innings.bowler:
            if innings.bowler.player.name == event.bowler.name:
                if innings.bowler.wickets == 4:  # This wicket makes it 5
                    return EventType.FIVE_WICKET_HAUL

        return current_type

    # =========================================================================
    # Tier Assignment — The Routing Decision
    # =========================================================================

    @staticmethod
    def _assign_tier(
        event: BallEvent,
        event_type: EventType,
        state: MatchState,
    ) -> EventTier:
        """Assign commentary tier (LLM vs template routing).

        MAJOR (→ LLM): boundaries, sixes, wickets, milestones, hat-tricks
        MINOR (→ template with variation): doubles, triples, extras
        ROUTINE (→ simple template): dot balls, singles
        AMBIENT (→ minimal): breaks, delays
        """
        # Always MAJOR — these deserve original commentary
        major_events = {
            EventType.BOUNDARY,
            EventType.SIX,
            EventType.WICKET,
            EventType.MILESTONE_50,
            EventType.MILESTONE_100,
            EventType.MILESTONE_150,
            EventType.MILESTONE_200,
            EventType.PARTNERSHIP_50,
            EventType.PARTNERSHIP_100,
            EventType.FIVE_WICKET_HAUL,
            EventType.HAT_TRICK,
            EventType.MAIDEN_OVER,
        }
        if event_type in major_events:
            return EventTier.MAJOR

        # Ambient — non-ball events
        ambient_events = {
            EventType.DRINKS_BREAK,
            EventType.RAIN_DELAY,
            EventType.INNINGS_BREAK,
        }
        if event_type in ambient_events:
            return EventTier.AMBIENT

        # Context-dependent upgrade: dot balls in death overs during a chase
        # are TENSE and deserve better commentary
        innings = state.get_current_innings()
        if innings and innings.target and event_type == EventType.DOT_BALL:
            runs_needed = innings.target - innings.total_runs
            if innings.total_overs >= 15 and runs_needed > 0:  # Death overs chase
                return EventTier.MINOR  # Upgrade from ROUTINE to MINOR

        # Minor events
        minor_events = {
            EventType.DOUBLE,
            EventType.TRIPLE,
            EventType.WIDE,
            EventType.NO_BALL,
            EventType.OVER_END,
        }
        if event_type in minor_events:
            return EventTier.MINOR

        return EventTier.ROUTINE

    # =========================================================================
    # Phase & Situation Detection
    # =========================================================================

    @staticmethod
    def _detect_phase(event: BallEvent, state: MatchState) -> InningsPhase:
        """Detect the current innings phase based on overs bowled and format."""
        innings = state.get_current_innings()
        overs = innings.total_overs if innings else event.over

        match state.match_info.format:
            case MatchFormat.T20 | MatchFormat.T10:
                if overs < 6:
                    return InningsPhase.POWERPLAY
                if overs < 15:
                    return InningsPhase.MIDDLE_OVERS
                return InningsPhase.DEATH_OVERS

            case MatchFormat.ODI:
                if overs < 10:
                    return InningsPhase.POWERPLAY_1
                if overs < 40:
                    return InningsPhase.MIDDLE_OVERS
                return InningsPhase.DEATH_OVERS

            case MatchFormat.TEST:
                if overs < 30:
                    return InningsPhase.EARLY
                if overs < 60:
                    return InningsPhase.MIDDLE
                return InningsPhase.LATE

            case _:
                return InningsPhase.MIDDLE_OVERS

    @staticmethod
    def _detect_situation(state: MatchState) -> MatchSituation:
        """Determine the high-level match situation for context-aware commentary."""
        innings = state.get_current_innings()
        if not innings:
            return MatchSituation.EVENLY_POISED

        # First innings — building or accelerating
        if innings.innings_number == 1:
            if innings.total_overs < 15:
                return MatchSituation.BATTING_FIRST_BUILDING
            return MatchSituation.BATTING_FIRST_ACCELERATING

        # Second innings — chasing
        if innings.target is not None:
            runs_needed = innings.target - innings.total_runs
            total_balls = 120  # T20 default
            balls_bowled = int(innings.total_overs * 6)
            balls_left = max(total_balls - balls_bowled, 1)
            required_rate = (runs_needed / balls_left) * 6

            current_rate = innings.run_rate

            if required_rate < 6:
                return MatchSituation.CHASING_COMFORTABLE
            if required_rate < current_rate + 2:
                return MatchSituation.CHASING_TIGHT
            return MatchSituation.CHASING_DESPERATE

        return MatchSituation.EVENLY_POISED

    # =========================================================================
    # Emotion Assignment
    # =========================================================================

    @staticmethod
    def _assign_emotion(
        event_type: EventType,
        tier: EventTier,
        situation: MatchSituation,
    ) -> EmotionTag:
        """Map event type + context to an emotional tone for TTS."""
        # Event-driven emotions
        emotion_map: dict[EventType, EmotionTag] = {
            EventType.SIX: EmotionTag.CELEBRATORY,
            EventType.BOUNDARY: EmotionTag.EXCITED,
            EventType.WICKET: EmotionTag.DRAMATIC,
            EventType.MILESTONE_50: EmotionTag.CELEBRATORY,
            EventType.MILESTONE_100: EmotionTag.CELEBRATORY,
            EventType.MILESTONE_150: EmotionTag.CELEBRATORY,
            EventType.MILESTONE_200: EmotionTag.CELEBRATORY,
            EventType.FIVE_WICKET_HAUL: EmotionTag.CELEBRATORY,
            EventType.HAT_TRICK: EmotionTag.CELEBRATORY,
            EventType.DOT_BALL: EmotionTag.NEUTRAL,
            EventType.SINGLE: EmotionTag.NEUTRAL,
            EventType.MAIDEN_OVER: EmotionTag.EXCITED,
        }

        base_emotion = emotion_map.get(event_type, EmotionTag.NEUTRAL)

        # Situation modifiers — tight situations make everything more tense
        if situation in (
            MatchSituation.CHASING_DESPERATE,
            MatchSituation.DEFENDING_DESPERATE,
        ):
            if base_emotion == EmotionTag.NEUTRAL:
                return EmotionTag.TENSE
            if event_type == EventType.WICKET:
                return EmotionTag.DRAMATIC
            if event_type in (EventType.BOUNDARY, EventType.SIX):
                return EmotionTag.CELEBRATORY

        return base_emotion

    @staticmethod
    def _max_overs(match_format: MatchFormat) -> int:
        """Maximum overs per innings for a given format."""
        return {
            MatchFormat.T20: 20,
            MatchFormat.ODI: 50,
            MatchFormat.TEST: 90,  # Per day, approximate
            MatchFormat.T10: 10,
            MatchFormat.THE_HUNDRED: 20,
        }.get(match_format, 20)

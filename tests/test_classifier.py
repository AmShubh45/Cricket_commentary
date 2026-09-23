"""Unit tests for the Event Classifier."""

from __future__ import annotations

import pytest

from cricket.domain.enums import (
    EmotionTag,
    EventTier,
    EventType,
    InningsPhase,
    MatchFormat,
    MatchSituation,
    MatchStatus,
)
from cricket.domain.models import (
    BallEvent,
    BatsmanState,
    InningsState,
    MatchInfo,
    MatchState,
    PlayerInfo,
    TeamInfo,
    WicketInfo,
)
from cricket.services.classifier.event_classifier import EventClassifier


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def classifier() -> EventClassifier:
    return EventClassifier()


@pytest.fixture
def team_ind() -> TeamInfo:
    return TeamInfo(id="ind", name="India", short_name="IND")


@pytest.fixture
def team_aus() -> TeamInfo:
    return TeamInfo(id="aus", name="Australia", short_name="AUS")


@pytest.fixture
def batsman() -> PlayerInfo:
    return PlayerInfo(id="1", name="Virat Kohli", short_name="Kohli")


@pytest.fixture
def bowler() -> PlayerInfo:
    return PlayerInfo(id="2", name="Mitchell Starc", short_name="Starc")


@pytest.fixture
def match_state(team_ind: TeamInfo, team_aus: TeamInfo) -> MatchState:
    """Standard T20 match state in the middle overs."""
    return MatchState(
        match_info=MatchInfo(
            match_id="test_001",
            title="India vs Australia, 1st T20I",
            format=MatchFormat.T20,
            team_home=team_ind,
            team_away=team_aus,
        ),
        status=MatchStatus.LIVE,
        current_innings=1,
        innings=[
            InningsState(
                innings_number=1,
                batting_team=team_ind,
                bowling_team=team_aus,
                total_runs=85,
                total_wickets=2,
                total_overs=10.3,
                run_rate=8.1,
                batsmen=[
                    BatsmanState(
                        player=PlayerInfo(id="1", name="Virat Kohli", short_name="Kohli"),
                        runs=30,
                        balls_faced=22,
                        fours=3,
                        sixes=1,
                        is_on_strike=True,
                    ),
                ],
            ),
        ],
    )


def _make_ball(
    batsman: PlayerInfo,
    bowler: PlayerInfo,
    runs: int = 0,
    is_boundary: bool = False,
    is_six: bool = False,
    wicket: WicketInfo | None = None,
    is_wide: bool = False,
    over: int = 10,
    ball: int = 4,
) -> BallEvent:
    """Helper to create a BallEvent for testing."""
    return BallEvent(
        match_id="test_001",
        innings=1,
        over=over,
        ball=ball,
        batsman=batsman,
        bowler=bowler,
        runs_batsman=runs,
        runs_total=runs,
        is_boundary=is_boundary,
        is_six=is_six,
        wicket=wicket,
        is_wide=is_wide,
    )


# =============================================================================
# Event Type Detection Tests
# =============================================================================


class TestEventTypeDetection:
    """Tests for _detect_event_type."""

    def test_dot_ball(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        match_state: MatchState,
    ) -> None:
        ball = _make_ball(batsman, bowler, runs=0)
        result = classifier.classify(ball, match_state)
        assert result.event_type == EventType.DOT_BALL

    def test_single(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        match_state: MatchState,
    ) -> None:
        ball = _make_ball(batsman, bowler, runs=1)
        result = classifier.classify(ball, match_state)
        assert result.event_type == EventType.SINGLE

    def test_boundary(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        match_state: MatchState,
    ) -> None:
        ball = _make_ball(batsman, bowler, runs=4, is_boundary=True)
        result = classifier.classify(ball, match_state)
        assert result.event_type == EventType.BOUNDARY

    def test_six(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        match_state: MatchState,
    ) -> None:
        ball = _make_ball(batsman, bowler, runs=6, is_six=True)
        result = classifier.classify(ball, match_state)
        assert result.event_type == EventType.SIX

    def test_wicket(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        match_state: MatchState,
    ) -> None:
        wicket = WicketInfo(wicket_type="bowled", batsman_out=batsman, bowler=bowler)
        ball = _make_ball(batsman, bowler, wicket=wicket)
        result = classifier.classify(ball, match_state)
        assert result.event_type == EventType.WICKET

    def test_wide(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        match_state: MatchState,
    ) -> None:
        ball = _make_ball(batsman, bowler, is_wide=True)
        result = classifier.classify(ball, match_state)
        assert result.event_type == EventType.WIDE


# =============================================================================
# Tier Assignment Tests
# =============================================================================


class TestTierAssignment:
    """Tests for tier routing (MAJOR → LLM, ROUTINE → template)."""

    def test_boundary_is_major(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        match_state: MatchState,
    ) -> None:
        ball = _make_ball(batsman, bowler, runs=4, is_boundary=True)
        result = classifier.classify(ball, match_state)
        assert result.tier == EventTier.MAJOR

    def test_six_is_major(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        match_state: MatchState,
    ) -> None:
        ball = _make_ball(batsman, bowler, runs=6, is_six=True)
        result = classifier.classify(ball, match_state)
        assert result.tier == EventTier.MAJOR

    def test_wicket_is_major(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        match_state: MatchState,
    ) -> None:
        wicket = WicketInfo(wicket_type="caught", batsman_out=batsman)
        ball = _make_ball(batsman, bowler, wicket=wicket)
        result = classifier.classify(ball, match_state)
        assert result.tier == EventTier.MAJOR

    def test_dot_ball_is_routine(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        match_state: MatchState,
    ) -> None:
        ball = _make_ball(batsman, bowler, runs=0)
        result = classifier.classify(ball, match_state)
        assert result.tier == EventTier.ROUTINE

    def test_single_is_routine(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        match_state: MatchState,
    ) -> None:
        ball = _make_ball(batsman, bowler, runs=1)
        result = classifier.classify(ball, match_state)
        assert result.tier == EventTier.ROUTINE

    def test_wide_is_minor(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        match_state: MatchState,
    ) -> None:
        ball = _make_ball(batsman, bowler, is_wide=True)
        result = classifier.classify(ball, match_state)
        assert result.tier == EventTier.MINOR


# =============================================================================
# Phase Detection Tests
# =============================================================================


class TestPhaseDetection:
    """Tests for innings phase detection."""

    def test_powerplay(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        team_ind: TeamInfo,
        team_aus: TeamInfo,
    ) -> None:
        state = MatchState(
            match_info=MatchInfo(
                match_id="t", title="t", format=MatchFormat.T20,
                team_home=team_ind, team_away=team_aus,
            ),
            status=MatchStatus.LIVE,
            current_innings=1,
            innings=[InningsState(
                innings_number=1, batting_team=team_ind, bowling_team=team_aus,
                total_overs=3.2,
            )],
        )
        ball = _make_ball(batsman, bowler, over=3, ball=2)
        result = classifier.classify(ball, state)
        assert result.phase == InningsPhase.POWERPLAY

    def test_death_overs(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        team_ind: TeamInfo,
        team_aus: TeamInfo,
    ) -> None:
        state = MatchState(
            match_info=MatchInfo(
                match_id="t", title="t", format=MatchFormat.T20,
                team_home=team_ind, team_away=team_aus,
            ),
            status=MatchStatus.LIVE,
            current_innings=1,
            innings=[InningsState(
                innings_number=1, batting_team=team_ind, bowling_team=team_aus,
                total_overs=17.4,
            )],
        )
        ball = _make_ball(batsman, bowler, over=17, ball=4)
        result = classifier.classify(ball, state)
        assert result.phase == InningsPhase.DEATH_OVERS


# =============================================================================
# Emotion Assignment Tests
# =============================================================================


class TestEmotionAssignment:
    """Tests for emotion tag assignment."""

    def test_six_is_celebratory(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        match_state: MatchState,
    ) -> None:
        ball = _make_ball(batsman, bowler, runs=6, is_six=True)
        result = classifier.classify(ball, match_state)
        assert result.emotion == EmotionTag.CELEBRATORY

    def test_wicket_is_dramatic(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        match_state: MatchState,
    ) -> None:
        wicket = WicketInfo(wicket_type="bowled", batsman_out=batsman)
        ball = _make_ball(batsman, bowler, wicket=wicket)
        result = classifier.classify(ball, match_state)
        assert result.emotion == EmotionTag.DRAMATIC

    def test_dot_ball_is_neutral(
        self,
        classifier: EventClassifier,
        batsman: PlayerInfo,
        bowler: PlayerInfo,
        match_state: MatchState,
    ) -> None:
        ball = _make_ball(batsman, bowler, runs=0)
        result = classifier.classify(ball, match_state)
        assert result.emotion == EmotionTag.NEUTRAL


# =============================================================================
# Milestone Detection Tests
# =============================================================================


class TestMilestoneDetection:
    """Tests for batsman/partnership milestone detection."""

    def test_fifty_milestone(
        self,
        classifier: EventClassifier,
        bowler: PlayerInfo,
        team_ind: TeamInfo,
        team_aus: TeamInfo,
    ) -> None:
        """When batsman is on 48 and scores 4, should detect MILESTONE_50."""
        batsman = PlayerInfo(id="1", name="Virat Kohli", short_name="Kohli")
        state = MatchState(
            match_info=MatchInfo(
                match_id="t", title="t", format=MatchFormat.T20,
                team_home=team_ind, team_away=team_aus,
            ),
            status=MatchStatus.LIVE,
            current_innings=1,
            innings=[InningsState(
                innings_number=1, batting_team=team_ind, bowling_team=team_aus,
                total_runs=85, total_wickets=2, total_overs=10.3,
                batsmen=[BatsmanState(player=batsman, runs=48, balls_faced=35)],
            )],
        )

        ball = _make_ball(batsman, bowler, runs=4, is_boundary=True)
        result = classifier.classify(ball, state)
        assert result.event_type == EventType.MILESTONE_50
        assert result.tier == EventTier.MAJOR
        assert result.emotion == EmotionTag.CELEBRATORY

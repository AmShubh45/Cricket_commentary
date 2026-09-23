"""Domain models for the cricket commentary system.

These are pure domain objects — no database ORM, no framework coupling.
They represent the core business entities that flow through the pipeline:
    Raw poll → BallEvent → CommentaryLine → AudioChunk → VideoFrame → Stream

All models are immutable (frozen) Pydantic models for thread-safety across
Celery workers and async tasks.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, computed_field

from cricket.domain.enums import (
    EmotionTag,
    EventTier,
    EventType,
    InningsPhase,
    MatchFormat,
    MatchSituation,
    MatchStatus,
    WicketType,
)


# =============================================================================
# Core Match & Player Models
# =============================================================================


class PlayerInfo(BaseModel, frozen=True):
    """Minimal player info needed for commentary generation."""

    id: str
    name: str
    short_name: str = ""  # e.g., "V Kohli" for templates
    team: str = ""

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display_name(self) -> str:
        return self.short_name or self.name


class TeamInfo(BaseModel, frozen=True):
    """Team information from the data feed."""

    id: str
    name: str
    short_name: str  # e.g., "IND", "AUS"
    logo_url: str = ""


class MatchInfo(BaseModel, frozen=True):
    """Static match metadata (doesn't change during the match)."""

    match_id: str
    title: str  # e.g., "India vs Australia, 2nd T20I"
    format: MatchFormat
    venue: str = ""
    city: str = ""
    team_home: TeamInfo
    team_away: TeamInfo
    start_time: datetime | None = None
    series_name: str = ""


# =============================================================================
# Ball Event — The Core Pipeline Input
# =============================================================================


class WicketInfo(BaseModel, frozen=True):
    """Details of how a wicket fell."""

    wicket_type: WicketType
    batsman_out: PlayerInfo
    fielder: PlayerInfo | None = None  # Catches, run-outs
    bowler: PlayerInfo | None = None   # Credit to bowler


class BallEvent(BaseModel, frozen=True):
    """A single ball delivery — the atomic unit flowing through the pipeline.

    This model is created by the Delta Detector when a new ball is detected
    in the data feed. Every subsequent pipeline stage operates on this.
    """

    # --- Identity ---
    match_id: str
    innings: int  # 1 or 2 (or 3/4 for Tests)
    over: int
    ball: int  # Ball number within the over (1-6, can exceed for extras)

    # --- Players ---
    batsman: PlayerInfo
    bowler: PlayerInfo
    non_striker: PlayerInfo | None = None

    # --- Outcome ---
    runs_batsman: int = 0  # Runs scored by batsman
    runs_extras: int = 0   # Extra runs (wides, no-balls, byes, leg-byes)
    runs_total: int = 0    # Total runs off this ball
    is_boundary: bool = False
    is_six: bool = False

    # --- Extras detail ---
    is_wide: bool = False
    is_no_ball: bool = False
    is_bye: bool = False
    is_leg_bye: bool = False

    # --- Wicket ---
    wicket: WicketInfo | None = None

    # --- Raw data from provider ---
    commentary_raw: str = ""  # Terse text from data provider
    raw_data: dict[str, Any] = Field(default_factory=dict)  # Full API response for this ball

    # --- Timestamp ---
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def over_display(self) -> str:
        """Human-readable over display, e.g., '4.3' for over 4, ball 3."""
        return f"{self.over}.{self.ball}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_dot_ball(self) -> bool:
        return self.runs_total == 0 and self.wicket is None and not self.is_wide and not self.is_no_ball

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_single(self) -> bool:
        return self.runs_batsman == 1 and not self.is_boundary


# =============================================================================
# Match State — Live Scorecard State in Redis
# =============================================================================


class BatsmanState(BaseModel):
    """Current batsman's live score."""

    player: PlayerInfo
    runs: int = 0
    balls_faced: int = 0
    fours: int = 0
    sixes: int = 0
    is_on_strike: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def strike_rate(self) -> float:
        if self.balls_faced == 0:
            return 0.0
        return round((self.runs / self.balls_faced) * 100, 2)


class BowlerState(BaseModel):
    """Current bowler's live figures."""

    player: PlayerInfo
    overs: float = 0.0
    maidens: int = 0
    runs_conceded: int = 0
    wickets: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def economy(self) -> float:
        if self.overs == 0:
            return 0.0
        return round(self.runs_conceded / self.overs, 2)


class InningsState(BaseModel):
    """Live state of a single innings."""

    innings_number: int
    batting_team: TeamInfo
    bowling_team: TeamInfo
    total_runs: int = 0
    total_wickets: int = 0
    total_overs: float = 0.0
    run_rate: float = 0.0
    required_rate: float | None = None  # Only for chasing innings
    target: int | None = None           # Only for chasing innings
    batsmen: list[BatsmanState] = Field(default_factory=list)
    bowler: BowlerState | None = None
    last_wicket: str = ""
    partnership_runs: int = 0
    partnership_balls: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def score_display(self) -> str:
        """e.g., '156/4 (15.3)' """
        return f"{self.total_runs}/{self.total_wickets} ({self.total_overs})"


class MatchState(BaseModel):
    """Complete live match state — stored in Redis, updated per-ball.

    This is the single source of truth for the scorecard renderer and
    the commentary context builder.
    """

    match_info: MatchInfo
    status: MatchStatus = MatchStatus.UPCOMING
    status_text: str = ""
    current_innings: int = 1
    innings: list[InningsState] = Field(default_factory=list)
    toss_winner: str = ""
    toss_decision: str = ""  # "bat" or "field"
    last_updated: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # --- Derived context for commentary ---
    recent_balls: list[BallEvent] = Field(default_factory=list, max_length=30)

    def get_current_innings(self) -> InningsState | None:
        """Get the currently active innings state."""
        for inn in self.innings:
            if inn.innings_number == self.current_innings:
                return inn
        return None


# =============================================================================
# Commentary Pipeline Models
# =============================================================================


class ClassifiedEvent(BaseModel, frozen=True):
    """Output of the Event Classifier — a BallEvent annotated with tier, type, and context."""

    ball_event: BallEvent
    event_type: EventType
    tier: EventTier
    phase: InningsPhase
    situation: MatchSituation
    emotion: EmotionTag

    # Context for LLM/template commentary generation
    batsman_runs: int = 0  # Batsman's current score
    batsman_balls: int = 0
    team_score: int = 0
    team_wickets: int = 0
    team_overs: float = 0.0
    target: int | None = None
    runs_needed: int | None = None
    balls_remaining: int | None = None
    partnership_runs: int = 0


class CommentaryLine(BaseModel, frozen=True):
    """A single line of generated Hindi commentary, ready for TTS.

    This is the output of either the Template Engine or the LLM Engine.
    """

    text: str  # Hindi commentary text
    emotion: EmotionTag
    event_type: EventType
    tier: EventTier
    source: str  # "template" or "llm"
    match_id: str
    over_display: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # Cost tracking
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cost_usd: float = 0.0


class AudioChunk(BaseModel, frozen=True):
    """A rendered TTS audio chunk, linked to its commentary line."""

    commentary: CommentaryLine
    audio_path: str  # Path to the audio file (MP3/WAV)
    duration_ms: int  # Audio duration in milliseconds
    tts_provider: str
    tts_cost_usd: float = 0.0


class VideoFrame(BaseModel, frozen=True):
    """A rendered scorecard frame (PNG screenshot from Playwright)."""

    match_id: str
    frame_path: str  # Path to the PNG file
    innings_state: InningsState
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


# =============================================================================
# Cost Tracking
# =============================================================================


class APICallCost(BaseModel):
    """Tracks cost of a single API call for budget monitoring."""

    provider: str  # "bedrock", "elevenlabs", "edge_tts", "cricketdata"
    operation: str  # "commentary_gen", "tts_render", "data_poll"
    cost_usd: float
    tokens_in: int = 0
    tokens_out: int = 0
    characters: int = 0
    match_id: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

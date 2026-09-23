"""Domain enumerations for the cricket commentary system.

These enums define the core vocabulary of the domain — event types, match formats,
innings phases, and provider identifiers. They are the foundation that every other
module imports from.
"""

from enum import StrEnum, auto


class EventTier(StrEnum):
    """Classification tier determining how commentary is generated.

    MAJOR events get LLM-generated original commentary (boundaries, wickets, milestones).
    MINOR events get template-based commentary with light variation.
    ROUTINE events get simple template fills (dot balls, singles).
    AMBIENT events are non-ball events (drinks, rain delay) with minimal commentary.
    """

    MAJOR = auto()
    MINOR = auto()
    ROUTINE = auto()
    AMBIENT = auto()


class EventType(StrEnum):
    """Specific ball/match event types from the data feed."""

    DOT_BALL = auto()
    SINGLE = auto()
    DOUBLE = auto()
    TRIPLE = auto()
    BOUNDARY = auto()
    SIX = auto()
    WICKET = auto()
    WIDE = auto()
    NO_BALL = auto()
    BYE = auto()
    LEG_BYE = auto()
    OVERTHROW = auto()
    MAIDEN_OVER = auto()
    OVER_END = auto()
    INNINGS_BREAK = auto()
    DRINKS_BREAK = auto()
    RAIN_DELAY = auto()
    MATCH_START = auto()
    MATCH_END = auto()
    MILESTONE_50 = auto()
    MILESTONE_100 = auto()
    MILESTONE_150 = auto()
    MILESTONE_200 = auto()
    PARTNERSHIP_50 = auto()
    PARTNERSHIP_100 = auto()
    FIVE_WICKET_HAUL = auto()
    HAT_TRICK = auto()


class WicketType(StrEnum):
    """How a wicket fell — affects commentary tone and template selection."""

    BOWLED = auto()
    CAUGHT = auto()
    CAUGHT_BEHIND = auto()
    CAUGHT_AND_BOWLED = auto()
    LBW = auto()
    STUMPED = auto()
    RUN_OUT = auto()
    HIT_WICKET = auto()
    RETIRED_HURT = auto()
    RETIRED_OUT = auto()
    TIMED_OUT = auto()
    OBSTRUCTING_FIELD = auto()


class MatchFormat(StrEnum):
    """Cricket match format — affects commentary style and pacing."""

    T20 = auto()
    ODI = auto()
    TEST = auto()
    T10 = auto()
    THE_HUNDRED = auto()


class InningsPhase(StrEnum):
    """Phase within an innings — affects commentary urgency and tone.

    For T20: powerplay (1-6), middle (7-15), death (16-20).
    For ODI: powerplay_1 (1-10), middle (11-40), death (41-50).
    """

    POWERPLAY = auto()
    POWERPLAY_1 = auto()
    POWERPLAY_2 = auto()
    POWERPLAY_3 = auto()
    MIDDLE_OVERS = auto()
    DEATH_OVERS = auto()
    EARLY = auto()      # Test match first session
    MIDDLE = auto()     # Test match middle session
    LATE = auto()       # Test match final session


class MatchSituation(StrEnum):
    """High-level match situation — for context-aware commentary."""

    BATTING_FIRST_BUILDING = auto()
    BATTING_FIRST_ACCELERATING = auto()
    CHASING_COMFORTABLE = auto()
    CHASING_TIGHT = auto()
    CHASING_DESPERATE = auto()
    DEFENDING_COMFORTABLE = auto()
    DEFENDING_TIGHT = auto()
    DEFENDING_DESPERATE = auto()
    EVENLY_POISED = auto()


class EmotionTag(StrEnum):
    """Emotion tags for TTS voice modulation.

    These map to ElevenLabs emotion tags but are provider-agnostic.
    Edge TTS uses prosody/rate adjustments instead.
    """

    NEUTRAL = auto()
    EXCITED = auto()
    DRAMATIC = auto()
    DISAPPOINTED = auto()
    CELEBRATORY = auto()
    TENSE = auto()
    CALM = auto()
    ANGRY = auto()


class DataFeedProvider(StrEnum):
    """Supported cricket data feed providers."""

    CRICKETDATA = "cricketdata"
    ENTITYSPORT = "entitysport"
    CRICBUZZ = "cricbuzz"


class TTSProvider(StrEnum):
    """Supported TTS providers."""

    EDGE_TTS = "edge_tts"
    ELEVENLABS = "elevenlabs"
    GOOGLE_CLOUD = "google_cloud"
    SARVAM = "sarvam"


class LLMProvider(StrEnum):
    """Supported LLM providers."""

    BEDROCK = "bedrock"
    OLLAMA = "ollama"
    FREE = "free"
    GEMINI = "gemini"
    GROQ = "groq"


class StreamStatus(StrEnum):
    """Status of the RTMP live stream."""

    IDLE = auto()
    STARTING = auto()
    LIVE = auto()
    PAUSED = auto()
    ERROR = auto()
    STOPPED = auto()


class MatchStatus(StrEnum):
    """Status of a cricket match from the data feed."""

    UPCOMING = auto()
    LIVE = auto()
    COMPLETED = auto()
    ABANDONED = auto()
    NO_RESULT = auto()

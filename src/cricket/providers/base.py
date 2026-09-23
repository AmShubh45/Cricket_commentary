"""Abstract provider interfaces (Strategy Pattern).

These Protocol classes define the contracts that all provider implementations
must satisfy. This is the key scalability mechanism — swapping Edge TTS → ElevenLabs,
or Nova Micro → Claude Haiku, requires ZERO changes to the pipeline code.

Design decision: We use typing.Protocol (structural subtyping) instead of
abc.ABC (nominal subtyping) because:
1. No inheritance required — any class with the right methods satisfies the contract
2. Better for dependency injection — easier to mock in tests
3. More Pythonic for duck-typing ecosystem
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from cricket.domain.models import (
    AudioChunk,
    BallEvent,
    CommentaryLine,
    MatchInfo,
    MatchState,
)


# =============================================================================
# Data Feed Provider — Cricket score data
# =============================================================================


@runtime_checkable
class DataFeedProvider(Protocol):
    """Contract for cricket data feed providers (CricketData, EntitySport, etc.).

    Implementations must handle:
    - API authentication
    - Rate limiting (respect provider quotas)
    - JSON → BallEvent parsing (provider-specific field mapping)
    - Error handling (network failures, malformed responses)
    """

    async def get_live_matches(self) -> list[MatchInfo]:
        """Fetch all currently live matches.

        Returns a list of MatchInfo for matches in progress.
        Used by the scheduler to start/stop polling for specific matches.
        """
        ...

    async def get_match_scorecard(self, match_id: str) -> MatchState:
        """Fetch the full current scorecard for a specific match.

        Returns the complete MatchState including both innings.
        Used for initial state hydration when joining a match in progress.
        """
        ...

    async def get_ball_by_ball(self, match_id: str) -> list[BallEvent]:
        """Fetch the ball-by-ball feed for a specific match.

        Returns balls in chronological order. The Delta Detector will
        compare this against Redis state to find new balls only.
        """
        ...

    async def healthcheck(self) -> bool:
        """Check if the data feed API is reachable and responding."""
        ...


# =============================================================================
# TTS Provider — Text-to-Speech
# =============================================================================


@runtime_checkable
class TTSProviderProtocol(Protocol):
    """Contract for TTS providers (Edge TTS, ElevenLabs, Google Cloud, etc.).

    Implementations must handle:
    - Hindi text input (Devanagari script)
    - Emotion/prosody modulation based on EmotionTag
    - Audio output in MP3/WAV format
    - Rate limiting and cost tracking
    """

    async def synthesize(self, commentary: CommentaryLine, output_path: str) -> AudioChunk:
        """Convert a commentary line to speech audio.

        Args:
            commentary: The Hindi text + emotion metadata to synthesize.
            output_path: Filesystem path where the audio file should be saved.

        Returns:
            AudioChunk with the audio file path and duration metadata.
        """
        ...

    async def healthcheck(self) -> bool:
        """Check if the TTS service is reachable."""
        ...

    def estimate_cost(self, text: str) -> float:
        """Estimate the USD cost of synthesizing the given text.

        Used for pre-flight cost checks and budget tracking.
        """
        ...


# =============================================================================
# LLM Provider — Commentary Generation
# =============================================================================


@runtime_checkable
class LLMProviderProtocol(Protocol):
    """Contract for LLM providers (AWS Bedrock, Ollama, etc.).

    Implementations must handle:
    - Hindi output generation
    - Structured prompt formatting with match context
    - Token counting and cost tracking
    - Langfuse trace integration
    """

    async def generate_commentary(
        self,
        prompt: str,
        match_context: dict,
        trace_id: str | None = None,
    ) -> CommentaryLine:
        """Generate original Hindi commentary for a notable cricket event.

        Args:
            prompt: The formatted prompt containing event details and instructions.
            match_context: Dict with current match state for grounding (score, batsman, etc.)
            trace_id: Optional Langfuse trace ID for observability.

        Returns:
            CommentaryLine with the generated Hindi text, token counts, and cost.
        """
        ...

    async def healthcheck(self) -> bool:
        """Check if the LLM service is reachable."""
        ...

    def estimate_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Estimate the USD cost for given token counts."""
        ...


# =============================================================================
# Graphics Renderer — Scorecard Screenshots
# =============================================================================


@runtime_checkable
class GraphicsRendererProtocol(Protocol):
    """Contract for scorecard graphics renderers.

    The default implementation uses Playwright (headless Chrome) to screenshot
    an HTML/CSS scorecard template. Alternative implementations could use
    Cairo/Pango directly, or a canvas-based Node.js renderer.
    """

    async def render_scorecard(self, match_state: MatchState, output_path: str) -> str:
        """Render the current scorecard as a PNG image.

        Args:
            match_state: The current live match state to render.
            output_path: Filesystem path where the PNG should be saved.

        Returns:
            The absolute path to the rendered PNG file.
        """
        ...

    async def initialize(self) -> None:
        """Initialize the renderer (e.g., launch Playwright browser)."""
        ...

    async def shutdown(self) -> None:
        """Clean up renderer resources (e.g., close browser)."""
        ...


# =============================================================================
# Stream Publisher — RTMP Output
# =============================================================================


@runtime_checkable
class StreamPublisherProtocol(Protocol):
    """Contract for live stream publishers (ffmpeg RTMP, etc.)."""

    async def start_stream(self, rtmp_url: str) -> None:
        """Start the RTMP output stream to the given URL."""
        ...

    async def push_frame(self, frame_path: str, audio_path: str | None = None) -> None:
        """Push a video frame (and optional audio) to the active stream."""
        ...

    async def stop_stream(self) -> None:
        """Gracefully stop the RTMP stream."""
        ...

    def is_streaming(self) -> bool:
        """Check if the stream is currently active."""
        ...

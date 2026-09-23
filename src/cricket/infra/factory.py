"""Provider Factory — Dependency Injection container.

Reads AppSettings and instantiates the correct provider implementations
based on configuration. This is the single place where provider selection
happens — all other code depends only on the abstract Protocol interfaces.

Swapping providers (e.g., Edge TTS → ElevenLabs) requires ONLY changing
an environment variable. No code changes.
"""

from __future__ import annotations

import logging

from config.settings import AppSettings, get_settings
from cricket.infra.cost_tracker import CostTracker
from cricket.infra.memory_store import InMemoryStateStore
from cricket.infra.redis_client import MatchStateStore
from cricket.providers.base import (
    DataFeedProvider,
    GraphicsRendererProtocol,
    LLMProviderProtocol,
    StreamPublisherProtocol,
    TTSProviderProtocol,
)
from cricket.providers.data_feed.cricbuzz import CricbuzzProvider
from cricket.providers.data_feed.cricketdata import CricketDataProvider
from cricket.providers.graphics.playwright_renderer import PlaywrightRenderer
from cricket.providers.llm.bedrock import BedrockLLMProvider
from cricket.providers.llm.free_llm import FreeLLMProvider
from cricket.providers.tts.edge_tts_provider import EdgeTTSProvider
from cricket.providers.tts.sarvam_tts_provider import SarvamTTSProvider
from cricket.services.classifier.event_classifier import EventClassifier
from cricket.services.commentary.engine import CommentaryOrchestrator
from cricket.services.commentary.template_engine import TemplateEngine
from cricket.services.pipeline.orchestrator import PipelineOrchestrator
from cricket.services.stream.ffmpeg_compositor import FFmpegCompositor, RTMPStreamer

logger = logging.getLogger(__name__)


class ProviderFactory:
    """Factory for creating provider instances based on configuration.

    Usage:
        factory = ProviderFactory()
        pipeline = factory.create_pipeline()
        await pipeline.start(match_id)
    """

    def __init__(self, settings: AppSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._instances: dict[str, object] = {}  # Cache for singleton providers

    def create_data_feed(self) -> DataFeedProvider:
        """Create the configured data feed provider."""
        provider_type = self._settings.data_feed.provider

        match provider_type.value:
            case "cricbuzz":
                logger.info("Creating Cricbuzz live data feed provider (free)")
                return CricbuzzProvider()

            case "cricketdata":
                logger.info("Creating CricketData provider")
                return CricketDataProvider(self._settings.data_feed)

            case "entitysport":
                # EntitySport provider not yet implemented
                raise NotImplementedError(
                    "EntitySport provider not yet implemented. "
                    "Set DATA_FEED_PROVIDER=cricbuzz or cricketdata in .env"
                )

            case _:
                raise ValueError(f"Unknown data feed provider: {provider_type}")

    def create_tts(self) -> TTSProviderProtocol:
        """Create the configured TTS provider."""
        provider_type = self._settings.tts.provider

        match provider_type.value:
            case "edge_tts":
                logger.info("Creating Edge TTS provider (free)")
                return EdgeTTSProvider(self._settings.tts)

            case "sarvam":
                logger.info("Creating Sarvam AI TTS provider (natural Hindi voice)")
                try:
                    return SarvamTTSProvider(self._settings.tts)
                except RuntimeError as e:
                    logger.warning("%s — falling back to Edge TTS.", e)
                    return EdgeTTSProvider(self._settings.tts)

            case "elevenlabs":
                # ElevenLabs provider — upgrade path
                try:
                    from cricket.providers.tts.elevenlabs_provider import ElevenLabsTTSProvider
                    logger.info("Creating ElevenLabs TTS provider")
                    return ElevenLabsTTSProvider(self._settings.tts)
                except ImportError:
                    logger.warning(
                        "ElevenLabs not installed. Install with: pip install cricket-commentary[elevenlabs]. "
                        "Falling back to Edge TTS."
                    )
                    return EdgeTTSProvider(self._settings.tts)

            case _:
                raise ValueError(f"Unknown TTS provider: {provider_type}")

    def create_llm(self) -> LLMProviderProtocol | None:
        """Create the configured LLM provider.

        Returns FreeLLMProvider for 'free', 'gemini', 'groq', or fallback.
        """
        provider_type = self._settings.llm.provider

        match provider_type.value:
            case "free" | "gemini" | "groq":
                logger.info("Creating Free LLM provider (Gemini / Groq / Smart fallback)")
                return FreeLLMProvider()

            case "bedrock":
                logger.info(
                    "Creating Bedrock LLM provider (model=%s)",
                    self._settings.llm.bedrock_model_id,
                )
                return BedrockLLMProvider(self._settings.llm)

            case "ollama":
                logger.info("Creating Ollama/Free LLM provider")
                return FreeLLMProvider()

            case _:
                logger.info("Defaulting to Free LLM provider")
                return FreeLLMProvider()

    def create_state_store(self) -> MatchStateStore | InMemoryStateStore:
        """Create the match state store.

        Tries Redis first; if unavailable, falls back to in-memory store
        so the pipeline works without Docker/Redis installed.
        """
        try:
            import redis as redis_sync
            r = redis_sync.from_url(self._settings.redis.url, socket_connect_timeout=2)
            r.ping()
            r.close()
            logger.info("Redis available — using Redis state store")
            return MatchStateStore(self._settings.redis)
        except Exception:
            logger.warning("Redis unavailable — using in-memory state store")
            return InMemoryStateStore()

    def create_classifier(self) -> EventClassifier:
        """Create the event classifier (stateless, no config needed)."""
        return EventClassifier()

    def create_template_engine(self) -> TemplateEngine:
        """Create the template engine with built-in + file-based templates."""
        return TemplateEngine(template_dir="templates/commentary")

    def create_commentary_orchestrator(self) -> CommentaryOrchestrator:
        """Create the commentary orchestrator with template + LLM engines."""
        template_engine = self.create_template_engine()
        llm_provider = self.create_llm()

        return CommentaryOrchestrator(
            template_engine=template_engine,
            llm_provider=llm_provider,
            monthly_llm_budget_usd=5.0,
        )

    def create_graphics_renderer(self) -> PlaywrightRenderer:
        """Create the Playwright-based scorecard renderer."""
        return PlaywrightRenderer(self._settings.graphics)

    def create_ffmpeg_compositor(self) -> FFmpegCompositor:
        """Create the ffmpeg offline video compositor."""
        return FFmpegCompositor(self._settings.ffmpeg)

    def create_rtmp_streamer(self) -> RTMPStreamer:
        """Create the live RTMP stream publisher."""
        return RTMPStreamer(self._settings.ffmpeg, self._settings.stream)

    def create_cost_tracker(self) -> CostTracker:
        """Create the cost tracker."""
        return CostTracker(self._settings.cost_tracking, self._settings.redis)

    def create_pipeline(
        self,
        with_graphics: bool = False,
        with_stream: bool = False,
    ) -> PipelineOrchestrator:
        """Create the full pipeline orchestrator with all dependencies wired.

        Args:
            with_graphics: Enable Playwright scorecard rendering.
            with_stream: Enable RTMP streaming (requires YouTube stream key).
        """
        data_feed = self.create_data_feed()
        classifier = self.create_classifier()
        commentary = self.create_commentary_orchestrator()
        tts = self.create_tts()
        state_store = self.create_state_store()

        graphics = self.create_graphics_renderer() if with_graphics else None
        stream = self.create_rtmp_streamer() if with_stream else None

        pipeline = PipelineOrchestrator(
            data_feed=data_feed,
            classifier=classifier,
            commentary=commentary,
            tts=tts,
            state_store=state_store,
            graphics=graphics,
            stream=stream,
            output_dir="output",
            poll_interval=self._settings.data_feed.poll_interval_seconds,
        )

        logger.info(
            "Pipeline created: data=%s, tts=%s, llm=%s, graphics=%s, stream=%s, poll=%ds",
            self._settings.data_feed.provider,
            self._settings.tts.provider,
            self._settings.llm.provider,
            "playwright" if with_graphics else "disabled",
            "rtmp" if with_stream else "disabled",
            self._settings.data_feed.poll_interval_seconds,
        )

        return pipeline

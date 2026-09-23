"""Pipeline Orchestrator — the main engine that drives the full pipeline.

This is the conductor that ties everything together:
    Data Poll → Delta Detect → Classify → Commentary → TTS → Render → Stream

It runs as a long-lived async loop (one per active match), coordinating
all services and providers. Designed to be run as a Celery task or
standalone async process.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

from cricket.domain.enums import EventTier, MatchStatus, StreamStatus
from cricket.domain.models import (
    AudioChunk,
    BallEvent,
    ClassifiedEvent,
    CommentaryLine,
    MatchState,
)
from cricket.infra.redis_client import MatchStateStore
from cricket.providers.base import (
    DataFeedProvider,
    GraphicsRendererProtocol,
    StreamPublisherProtocol,
    TTSProviderProtocol,
)
from cricket.services.classifier.event_classifier import EventClassifier
from cricket.services.commentary.engine import CommentaryOrchestrator

logger = logging.getLogger(__name__)


class PipelineOrchestrator:
    """Orchestrates the full commentary pipeline for a single match.

    Lifecycle:
    1. start(match_id) — begins polling and processing
    2. Runs in a loop: poll → detect new balls → classify → comment → tts → render
    3. stop() — gracefully shuts down

    Usage:
        orchestrator = PipelineOrchestrator(
            data_feed=cricketdata_provider,
            classifier=event_classifier,
            commentary=commentary_orchestrator,
            tts=edge_tts_provider,
            state_store=redis_state_store,
            output_dir="/app/output",
        )
        await orchestrator.start(match_id="abc123", poll_interval=30)
    """

    def __init__(
        self,
        data_feed: DataFeedProvider,
        classifier: EventClassifier,
        commentary: CommentaryOrchestrator,
        tts: TTSProviderProtocol,
        state_store: MatchStateStore,
        graphics: GraphicsRendererProtocol | None = None,
        stream: StreamPublisherProtocol | None = None,
        output_dir: str = "output",
        poll_interval: int = 30,
    ) -> None:
        self._data_feed = data_feed
        self._classifier = classifier
        self._commentary = commentary
        self._tts = tts
        self._state_store = state_store
        self._graphics = graphics
        self._stream = stream
        self._output_dir = Path(output_dir)
        self._poll_interval = poll_interval

        self._status = StreamStatus.IDLE
        self._running = False
        self._match_id: str = ""
        self._ball_count = 0  # For naming output files

        # Ensure output directories exist
        for subdir in ("audio", "frames", "video"):
            (self._output_dir / subdir).mkdir(parents=True, exist_ok=True)

    @property
    def status(self) -> StreamStatus:
        return self._status

    async def start(self, match_id: str, poll_interval: int | None = None) -> None:
        """Start the pipeline loop for a specific match.

        This is the main entry point. It runs until the match ends
        or stop() is called.
        """
        self._match_id = match_id
        self._running = True
        self._status = StreamStatus.STARTING

        if poll_interval is not None:
            self._poll_interval = poll_interval

        logger.info(
            "Pipeline starting for match %s (poll every %ds)",
            match_id,
            self._poll_interval,
        )

        # Initialize graphics renderer if available
        if self._graphics:
            await self._graphics.initialize()

        try:
            # Hydrate initial match state
            match_state = await self._data_feed.get_match_scorecard(match_id)
            await self._state_store.save_state(match_id, match_state)

            self._status = StreamStatus.LIVE
            logger.info("Pipeline LIVE for match %s", match_id)

            # Main polling loop
            while self._running:
                try:
                    await self._poll_and_process(match_id)
                except Exception:
                    logger.exception("Error in pipeline loop for match %s", match_id)
                    self._status = StreamStatus.ERROR
                    # Don't crash — recover and continue
                    await asyncio.sleep(5)
                    self._status = StreamStatus.LIVE

                await asyncio.sleep(self._poll_interval)

                # Check if match has ended
                state = await self._state_store.get_state(match_id)
                if state and state.status == MatchStatus.COMPLETED:
                    logger.info("Match %s completed. Pipeline shutting down.", match_id)
                    break

        finally:
            self._status = StreamStatus.STOPPED
            self._running = False
            if self._graphics:
                await self._graphics.shutdown()
            if self._stream:
                await self._stream.stop_stream()
            logger.info("Pipeline stopped for match %s", match_id)

    async def stop(self) -> None:
        """Gracefully stop the pipeline."""
        logger.info("Pipeline stop requested for match %s", self._match_id)
        self._running = False

    async def _poll_and_process(self, match_id: str) -> None:
        """Single iteration of the poll-process loop.

        1. Fetch latest ball-by-ball data
        2. Detect new balls (delta detection)
        3. For each new ball: classify → generate commentary → TTS → render
        """
        # 1. Fetch latest data
        new_balls = await self._data_feed.get_ball_by_ball(match_id)
        if not new_balls:
            return

        # 2. Delta detection — find balls we haven't processed yet
        current_state = await self._state_store.get_state(match_id)
        if not current_state:
            logger.warning("No state found for match %s, fetching fresh scorecard", match_id)
            current_state = await self._data_feed.get_match_scorecard(match_id)
            await self._state_store.save_state(match_id, current_state)

        unseen_balls = await self._detect_new_balls(match_id, new_balls)

        if not unseen_balls:
            return

        logger.info(
            "Match %s: %d new balls detected",
            match_id,
            len(unseen_balls),
        )

        # 3. Process each new ball through the pipeline
        for ball in unseen_balls:
            await self._process_ball(ball, current_state)

            # Update match state from scorecard after processing
            try:
                updated_state = await self._data_feed.get_match_scorecard(match_id)
                await self._state_store.save_state(match_id, updated_state)
                current_state = updated_state
            except Exception:
                logger.warning("Failed to update scorecard after ball, using stale state")

    async def _detect_new_balls(
        self,
        match_id: str,
        balls: list[BallEvent],
    ) -> list[BallEvent]:
        """Compare fetched balls against last processed ball in Redis.

        Uses the over.ball as a sequence key to identify new deliveries.
        """
        last_processed = await self._state_store.get_last_processed_ball(match_id)

        if last_processed is None:
            # First poll — process only the most recent ball to avoid replaying history
            return balls[-1:] if balls else []

        # Filter to only balls AFTER the last processed one
        unseen = []
        for ball in balls:
            ball_key = f"{ball.innings}_{ball.over}_{ball.ball}"
            if ball_key > last_processed:
                unseen.append(ball)

        return unseen

    async def _process_ball(self, ball: BallEvent, state: MatchState) -> None:
        """Process a single ball through the full pipeline.

        classify → commentary → TTS → (optional: render graphics) → (optional: stream)
        """
        self._ball_count += 1
        ball_id = f"{ball.innings}_{ball.over}_{ball.ball}"

        logger.info(
            "Processing ball %s: %s → %s (%d runs)",
            ball_id,
            ball.bowler.display_name,
            ball.batsman.display_name,
            ball.runs_total,
        )

        # 1. Classify the event
        classified = self._classifier.classify(ball, state)
        logger.info(
            "Classified: %s | tier=%s | emotion=%s",
            classified.event_type,
            classified.tier,
            classified.emotion,
        )

        # 2. Generate commentary
        commentary = await self._commentary.generate(classified, state)
        logger.info("Commentary [%s]: %s", commentary.source, commentary.text)

        # 3. TTS — convert to audio
        audio_path = str(
            self._output_dir / "audio" / f"ball_{self._ball_count:04d}_{ball_id}.mp3"
        )
        audio_chunk = await self._tts.synthesize(commentary, audio_path)
        logger.info(
            "TTS: %dms audio generated at %s (cost=$%.4f)",
            audio_chunk.duration_ms,
            audio_chunk.audio_path,
            audio_chunk.tts_cost_usd,
        )

        # 4. Render scorecard frame (if graphics renderer available)
        if self._graphics:
            frame_path = str(
                self._output_dir / "frames" / f"frame_{self._ball_count:04d}_{ball_id}.png"
            )
            await self._graphics.render_scorecard(state, frame_path)

        # 5. Push to stream (if stream publisher available)
        if self._stream and self._stream.is_streaming():
            frame_to_push = str(
                self._output_dir / "frames" / f"frame_{self._ball_count:04d}_{ball_id}.png"
            )
            await self._stream.push_frame(frame_to_push, audio_chunk.audio_path)

        # 6. Mark ball as processed in Redis
        await self._state_store.set_last_processed_ball(
            self._match_id,
            ball_id,
        )

    async def process_replay(
        self,
        match_id: str,
        balls: list[BallEvent],
        state: MatchState,
        delay_between_balls: float = 2.0,
    ) -> list[AudioChunk]:
        """Replay mode — process a list of historical balls with a configurable delay.

        Used in Phase 1 (offline MVP) to prove the pipeline end-to-end
        without live data pressure. Updates match state progressively
        so scores/wickets are correct in commentary.

        Returns the list of generated AudioChunks for stitching into a video.
        """
        logger.info("Replay mode: processing %d balls for match %s", len(balls), match_id)
        self._match_id = match_id
        audio_chunks: list[AudioChunk] = []

        await self._state_store.save_state(match_id, state)

        for ball in balls:
            self._ball_count += 1
            ball_id = f"{ball.innings}_{ball.over}_{ball.ball}"

            # Classify
            classified = self._classifier.classify(ball, state)

            # Commentary
            commentary = await self._commentary.generate(classified, state)

            # TTS
            audio_path = str(
                self._output_dir / "audio" / f"replay_{self._ball_count:04d}_{ball_id}.mp3"
            )
            audio_chunk = await self._tts.synthesize(commentary, audio_path)
            audio_chunks.append(audio_chunk)

            logger.info(
                "Replay ball %s: [%s] %s → %dms audio",
                ball_id,
                classified.event_type,
                commentary.text[:60],
                audio_chunk.duration_ms,
            )

            # --- UPDATE MATCH STATE after this ball ---
            self._update_state_from_ball(state, ball)

            # Simulate real-time pacing
            await asyncio.sleep(delay_between_balls)

        logger.info(
            "Replay complete: %d balls processed, %d audio chunks generated",
            len(balls),
            len(audio_chunks),
        )
        return audio_chunks

    @staticmethod
    def _update_state_from_ball(state: MatchState, ball: BallEvent) -> None:
        """Update match state in-place after processing a ball.

        Keeps scores, wickets, overs, and batsman stats accurate
        so that the next ball's commentary references the correct score.
        """
        innings = state.get_current_innings()
        if not innings:
            return

        # Update total score
        innings.total_runs += ball.runs_total
        if ball.wicket:
            innings.total_wickets += 1

        # Update overs display (e.g., over=1, ball=3 → 1.3 overs)
        innings.total_overs = float(f"{ball.over}.{ball.ball}")

        # Update run rate
        completed_overs = ball.over + (ball.ball / 6.0)
        if completed_overs > 0:
            innings.run_rate = round(innings.total_runs / completed_overs, 2)

        # Update partnership
        if ball.wicket:
            innings.partnership_runs = 0
            innings.partnership_balls = 0
        else:
            innings.partnership_runs += ball.runs_total
            innings.partnership_balls += 1

        # Update batsman stats (find matching batsman in state)
        for batter in innings.batsmen:
            if batter.player.id == ball.batsman.id or batter.player.name == ball.batsman.name:
                batter.runs += ball.runs_batsman
                batter.balls_faced += 1
                if ball.is_boundary:
                    batter.fours += 1
                if ball.is_six:
                    batter.sixes += 1
                break

        # Track recent balls
        state.recent_balls = (state.recent_balls + [ball])[-30:]


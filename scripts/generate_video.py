"""Generate a full highlights video from a simulated match.

This script runs the COMPLETE Phase 1 pipeline end-to-end:
1. Simulate a 3-over India vs Australia T20 match (18 balls)
2. For each ball: classify → generate commentary → TTS audio
3. Render scorecard frames via Playwright
4. Stitch frames + audio into a single MP4 video

Usage:
    python scripts/generate_video.py

Output:
    output/video/highlights.mp4

Prerequisites:
    - Redis running (docker compose up -d redis)
    - Playwright installed (playwright install chromium)
    - ffmpeg installed
    - No API keys needed (uses simulated data + Edge TTS)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.replay_match import create_simulated_match
from cricket.infra.factory import ProviderFactory
from cricket.providers.graphics.playwright_renderer import PlaywrightRenderer
from cricket.services.stream.ffmpeg_compositor import FFmpegCompositor
from config.settings import get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
logger = logging.getLogger("generate_video")


async def main() -> None:
    """Generate a full highlights video from simulated match data."""
    settings = get_settings()

    logger.info("=" * 60)
    logger.info("CRICKET COMMENTARY — FULL VIDEO GENERATION")
    logger.info("Phase 1 MVP Demo: Simulated IND vs AUS T20")
    logger.info("=" * 60)

    # --- Step 1: Create simulated match data ---
    match_state, balls = create_simulated_match()
    logger.info("Match: %s (%d balls)", match_state.match_info.title, len(balls))

    # --- Step 2: Create pipeline components ---
    factory = ProviderFactory()
    pipeline = factory.create_pipeline(with_graphics=False)

    # --- Step 3: Run replay — generates commentary + TTS audio ---
    logger.info("\n--- REPLAY: Generating commentary & audio ---")
    audio_chunks = await pipeline.process_replay(
        match_id=match_state.match_info.match_id,
        balls=balls,
        state=match_state,
        delay_between_balls=0.5,  # Fast for demo
    )

    if not audio_chunks:
        logger.error("No audio chunks generated. Aborting.")
        return

    logger.info("Generated %d audio chunks", len(audio_chunks))

    # --- Step 4: Render scorecard frames via Playwright ---
    logger.info("\n--- RENDERING: Generating scorecard frames ---")
    renderer = PlaywrightRenderer(settings.graphics)

    frames: list[tuple[str, float]] = []
    audio_files: list[str] = []

    try:
        await renderer.initialize()

        for idx, chunk in enumerate(audio_chunks):
            ball = balls[idx] if idx < len(balls) else balls[-1]

            # Update match state for this ball (simplified for demo)
            await renderer.update_commentary_ticker(
                commentary_text=chunk.commentary.text,
                event_label=chunk.commentary.event_type.value.upper(),
                event_type=chunk.commentary.event_type.value,
            )

            # Add ball to over strip
            await renderer.add_ball_to_over_strip(
                runs=ball.runs_total,
                is_wicket=ball.wicket is not None,
                is_wide=ball.is_wide,
            )

            # Clear over strip at new over
            if idx > 0 and ball.over != balls[idx - 1].over:
                await renderer.clear_over_strip()

            # Render scorecard frame
            frame_path = f"output/frames/video_frame_{idx:04d}.png"
            await renderer.render_scorecard(match_state, frame_path)

            # Duration = audio duration (in seconds)
            duration_sec = max(chunk.duration_ms / 1000.0, 1.5)

            frames.append((frame_path, duration_sec))
            audio_files.append(chunk.audio_path)

            logger.info(
                "  Frame %d: %s | %.1fs | %s",
                idx + 1,
                chunk.commentary.event_type.value,
                duration_sec,
                chunk.commentary.text[:50],
            )

    finally:
        await renderer.shutdown()

    logger.info("Rendered %d frames", len(frames))

    # --- Step 5: Compose video with ffmpeg ---
    logger.info("\n--- COMPOSITING: Stitching video ---")
    compositor = FFmpegCompositor(settings.ffmpeg)

    output_path = "output/video/highlights.mp4"
    try:
        result = await compositor.create_video(
            frames=frames,
            audio_files=audio_files,
            output_path=output_path,
        )
        logger.info("Video created: %s", result)
    except Exception:
        logger.exception("Video compositing failed")
        logger.info(
            "Audio files are still available at output/audio/ — "
            "you can manually stitch with ffmpeg"
        )
        return

    # --- Summary ---
    total_duration_s = sum(f[1] for f in frames)
    total_cost = sum(c.tts_cost_usd + c.commentary.llm_cost_usd for c in audio_chunks)

    logger.info("\n" + "=" * 60)
    logger.info("VIDEO GENERATION COMPLETE")
    logger.info("=" * 60)
    logger.info("Output:    %s", output_path)
    logger.info("Balls:     %d", len(balls))
    logger.info("Duration:  %.1f seconds", total_duration_s)
    logger.info("Cost:      $%.6f (₹%.2f)", total_cost, total_cost * 84)
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())

"""Live Cricket Commentary Runner (100% Free / Zero API Keys / No Bedrock).

Polls Cricbuzz live commentary, detects new balls in real-time,
generates professional Hindi commentary (Gemini / Groq / Smart Engine),
synthesizes natural Hindi audio (Edge TTS), and renders 1080p scorecard video clips (Playwright + FFmpeg).

Usage:
    # Process only the single latest live ball (quick test):
    python scripts/run_live_cricbuzz.py --once

    # Run continuous live match loop (polls every 15s):
    python scripts/run_live_cricbuzz.py

    # Audio-only mode (fast, skips video generation):
    python scripts/run_live_cricbuzz.py --audio-only
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

# Ensure UTF-8 console output for Hindi Devanagari script on Windows
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# Add project root and src directory to Python path
ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "src"))

from config.settings import get_settings
from cricket.domain.enums import EmotionTag, EventTier, EventType
from cricket.domain.models import BallEvent, CommentaryLine
from cricket.infra.factory import ProviderFactory
from cricket.infra.memory_store import InMemoryStateStore
from cricket.providers.data_feed.cricbuzz import CricbuzzProvider
from cricket.providers.llm.free_llm import FreeLLMProvider
from cricket.services.classifier.event_classifier import EventClassifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("LiveCricbuzz")


async def run_live_pipeline(
    match_id: str = "163061",
    poll_interval: int = 15,
    once: bool = False,
    audio_only: bool = False,
) -> None:
    """Run the live commentary loop for the given Cricbuzz match."""
    output_dir = ROOT_DIR / "output" / "live"
    output_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 70)
    print(f"🏏 CRICKET LIVE COMMENTARY — MATCH {match_id}")
    print("   East Zone vs South Zone (Final, Duleep Trophy 2026)")
    print("   Powered by Cricbuzz + Free Hindi Engine + Edge TTS")
    print("=" * 70 + "\n")

    # Settings and Factory
    settings = get_settings()
    factory = ProviderFactory(settings)

    # Initialize providers
    data_provider = CricbuzzProvider(default_match_id=match_id)
    classifier = EventClassifier()
    llm_provider = FreeLLMProvider()
    tts_provider = factory.create_tts()

    # Playwright & FFmpeg setup (if video enabled)
    renderer = None
    compositor = None
    if not audio_only:
        try:
            renderer = factory.create_graphics_renderer()
            await renderer.initialize()
            compositor = factory.create_ffmpeg_compositor()
            logger.info("Playwright & FFmpeg video compositor initialized")
        except Exception as e:
            logger.warning("Could not initialize video renderer (%s). Running audio-only.", e)
            audio_only = True
            renderer = None
            compositor = None

    # Initial hydration: fetch scorecard and ball feed
    scorecard = await data_provider.get_match_scorecard(match_id)
    innings = scorecard.get_current_innings()
    if innings:
        print(f"📊 Current Match State: {scorecard.match_info.title}")
        print(f"   Score: {innings.score_display} | Run Rate: {innings.run_rate}")
        for b in innings.batsmen:
            print(f"   🏏 Batter: {b.player.name} {b.runs} ({b.balls_faced}b, {b.fours}x4, {b.sixes}x6)")
        if innings.bowler:
            print(f"   🎯 Bowler: {innings.bowler.player.name} ({innings.bowler.overs}-{innings.bowler.maidens}-{innings.bowler.runs_conceded}-{innings.bowler.wickets})")
        print("-" * 70)

    # Track processed balls
    processed_ball_keys: set[str] = set()

    try:
        while True:
            try:
                # Fetch recent balls
                balls = await data_provider.get_ball_by_ball(match_id)
                current_card = await data_provider.get_match_scorecard(match_id)
                cur_innings = current_card.get_current_innings()

                # If running '--once', only process the single latest ball
                balls_to_check = [balls[-1]] if (once and balls) else balls

                for ball in balls_to_check:
                    ball_key = f"{ball.innings}_{ball.over}_{ball.ball}"
                    if ball_key in processed_ball_keys:
                        continue

                    processed_ball_keys.add(ball_key)

                    print(f"\n⚡ NEW BALL DELIVERED: Over {ball.over_display}")
                    print(f"   Bowler: {ball.bowler.name} ➡️ Batter: {ball.batsman.name}")
                    print(f"   Raw Text: {ball.commentary_raw}")

                    # Classify event
                    event_type = EventType.DOT_BALL
                    emotion = EmotionTag.NEUTRAL
                    if ball.is_six:
                        event_type = EventType.SIX
                        emotion = EmotionTag.EXCITED
                    elif ball.is_boundary:
                        event_type = EventType.BOUNDARY
                        emotion = EmotionTag.EXCITED
                    elif ball.wicket:
                        event_type = EventType.WICKET
                        emotion = EmotionTag.DRAMATIC
                    elif ball.is_single:
                        event_type = EventType.SINGLE
                    elif ball.runs_batsman > 1:
                        event_type = EventType.DOUBLE

                    # Build context for commentary
                    ctx = {
                        "match_id": match_id,
                        "over_display": ball.over_display,
                        "bowler": ball.bowler.name,
                        "batsman": ball.batsman.name,
                        "runs_batsman": ball.runs_batsman,
                        "batsman_runs": cur_innings.batsmen[0].runs if (cur_innings and cur_innings.batsmen) else 0,
                        "team_score": cur_innings.total_runs if cur_innings else 0,
                        "team_wickets": cur_innings.total_wickets if cur_innings else 0,
                        "emotion": emotion.value,
                        "event_type": event_type.value,
                    }

                    # Generate Hindi commentary
                    commentary: CommentaryLine = await llm_provider.generate_commentary(
                        ball.commentary_raw, ctx
                    )

                    print(f"\n🎙️ [HINDI COMMENTARY ({commentary.source})]:")
                    print(f"   \"{commentary.text}\"")

                    # Synthesize speech via Edge TTS
                    safe_over = ball.over_display.replace(".", "_")
                    audio_filename = f"ball_{safe_over}_{int(datetime.now().timestamp())}.mp3"
                    audio_path = output_dir / audio_filename

                    audio_chunk = await tts_provider.synthesize(commentary, str(audio_path))
                    duration_sec = max(audio_chunk.duration_ms / 1000.0, 1.5)
                    print(f"🔊 Audio generated: {audio_chunk.audio_path} ({duration_sec:.1f}s)")

                    # Video generation
                    if not audio_only and renderer and compositor:
                        try:
                            frame_path = output_dir / f"frame_{safe_over}.png"
                            await renderer.render_scorecard(current_card, str(frame_path))

                            video_path = output_dir / f"clip_{safe_over}.mp4"
                            await compositor.create_video(
                                frames=[(str(frame_path), duration_sec)],
                                audio_files=[str(audio_chunk.audio_path)],
                                output_path=str(video_path),
                            )
                            print(f"🎬 Video clip created: {video_path}")
                        except Exception as e:
                            logger.warning("Failed to render video clip: %s", e)

                    print("-" * 70)

                if once:
                    print("\n✅ Processed latest ball successfully (--once mode). Exiting.")
                    break

                # Sleep until next poll
                await asyncio.sleep(poll_interval)

            except Exception as e:
                logger.error("Error during poll cycle: %s", e, exc_info=True)
                await asyncio.sleep(poll_interval)

    except KeyboardInterrupt:
        print("\n🛑 Stopped live commentary.")
    finally:
        await data_provider.close()
        await llm_provider.close()
        if renderer:
            await renderer.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description="Live Cricket Commentary Runner (Free)")
    parser.add_argument("--match-id", default="163061", help="Cricbuzz Match ID (default: 163061)")
    parser.add_argument("--poll-interval", type=int, default=15, help="Poll interval in seconds (default: 15)")
    parser.add_argument("--once", action="store_true", help="Process only the single latest live ball and exit")
    parser.add_argument("--audio-only", action="store_true", help="Generate audio commentary without rendering video")

    args = parser.parse_args()
    asyncio.run(
        run_live_pipeline(
            match_id=args.match_id,
            poll_interval=args.poll_interval,
            once=args.once,
            audio_only=args.audio_only,
        )
    )


if __name__ == "__main__":
    main()

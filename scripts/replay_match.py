"""Match Replay Script — Phase 1 MVP testing.

Replays a completed match's ball-by-ball data through the full pipeline
at configurable speed. Generates commentary audio for every ball.

Usage:
    python -m scripts.replay_match --match-id <id> --speed 5.0

Or with simulated data (no API key needed):
    python -m scripts.replay_match --simulate
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

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
from cricket.infra.factory import ProviderFactory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)


def create_simulated_match() -> tuple[MatchState, list[BallEvent]]:
    """Create a simulated India vs Australia T20 match for testing.

    Returns a match state and a list of ball events that simulate
    a realistic T20 innings.
    """
    team_india = TeamInfo(id="ind", name="India", short_name="IND")
    team_aus = TeamInfo(id="aus", name="Australia", short_name="AUS")

    match_info = MatchInfo(
        match_id="sim_001",
        title="India vs Australia, 1st T20I",
        format=MatchFormat.T20,
        venue="Wankhede Stadium, Mumbai",
        team_home=team_india,
        team_away=team_aus,
    )

    innings_state = InningsState(
        innings_number=1,
        batting_team=team_india,
        bowling_team=team_aus,
        total_runs=0,
        total_wickets=0,
        total_overs=0.0,
        batsmen=[
            BatsmanState(
                player=PlayerInfo(id="1", name="रोहित शर्मा", short_name="रोहित"),
                is_on_strike=True,
            ),
            BatsmanState(
                player=PlayerInfo(id="2", name="यशस्वी जायसवाल", short_name="जायसवाल"),
            ),
        ],
    )

    match_state = MatchState(
        match_info=match_info,
        status=MatchStatus.LIVE,
        current_innings=1,
        innings=[innings_state],
    )

    # Simulate ball events for first 3 overs (18 balls)
    batsmen = [
        PlayerInfo(id="1", name="रोहित शर्मा", short_name="रोहित"),
        PlayerInfo(id="2", name="यशस्वी जायसवाल", short_name="जायसवाल"),
    ]
    bowlers = [
        PlayerInfo(id="10", name="Mitchell Starc", short_name="Starc"),
        PlayerInfo(id="11", name="Josh Hazlewood", short_name="Hazlewood"),
        PlayerInfo(id="12", name="Pat Cummins", short_name="Cummins"),
    ]

    balls: list[BallEvent] = [
        # Over 1 — Starc to Rohit: dot, single, dot, FOUR, dot, SIX
        BallEvent(match_id="sim_001", innings=1, over=0, ball=1,
                  batsman=batsmen[0], bowler=bowlers[0],
                  runs_batsman=0, runs_total=0),
        BallEvent(match_id="sim_001", innings=1, over=0, ball=2,
                  batsman=batsmen[0], bowler=bowlers[0],
                  runs_batsman=1, runs_total=1),
        BallEvent(match_id="sim_001", innings=1, over=0, ball=3,
                  batsman=batsmen[1], bowler=bowlers[0],
                  runs_batsman=0, runs_total=0),
        BallEvent(match_id="sim_001", innings=1, over=0, ball=4,
                  batsman=batsmen[1], bowler=bowlers[0],
                  runs_batsman=4, runs_total=4, is_boundary=True),
        BallEvent(match_id="sim_001", innings=1, over=0, ball=5,
                  batsman=batsmen[1], bowler=bowlers[0],
                  runs_batsman=0, runs_total=0),
        BallEvent(match_id="sim_001", innings=1, over=0, ball=6,
                  batsman=batsmen[1], bowler=bowlers[0],
                  runs_batsman=6, runs_total=6, is_six=True),

        # Over 2 — Hazlewood to Rohit: single, FOUR, dot, dot, WICKET, dot
        BallEvent(match_id="sim_001", innings=1, over=1, ball=1,
                  batsman=batsmen[0], bowler=bowlers[1],
                  runs_batsman=1, runs_total=1),
        BallEvent(match_id="sim_001", innings=1, over=1, ball=2,
                  batsman=batsmen[1], bowler=bowlers[1],
                  runs_batsman=4, runs_total=4, is_boundary=True),
        BallEvent(match_id="sim_001", innings=1, over=1, ball=3,
                  batsman=batsmen[1], bowler=bowlers[1],
                  runs_batsman=0, runs_total=0),
        BallEvent(match_id="sim_001", innings=1, over=1, ball=4,
                  batsman=batsmen[1], bowler=bowlers[1],
                  runs_batsman=0, runs_total=0),
        BallEvent(match_id="sim_001", innings=1, over=1, ball=5,
                  batsman=batsmen[1], bowler=bowlers[1],
                  runs_batsman=0, runs_total=0,
                  wicket=WicketInfo(
                      wicket_type="bowled",
                      batsman_out=batsmen[1],
                      bowler=bowlers[1],
                  )),
        BallEvent(match_id="sim_001", innings=1, over=1, ball=6,
                  batsman=batsmen[0], bowler=bowlers[1],
                  runs_batsman=0, runs_total=0),

        # Over 3 — Cummins to Rohit: SIX, single, FOUR, dot, double, single
        BallEvent(match_id="sim_001", innings=1, over=2, ball=1,
                  batsman=batsmen[0], bowler=bowlers[2],
                  runs_batsman=6, runs_total=6, is_six=True),
        BallEvent(match_id="sim_001", innings=1, over=2, ball=2,
                  batsman=batsmen[0], bowler=bowlers[2],
                  runs_batsman=1, runs_total=1),
        BallEvent(match_id="sim_001", innings=1, over=2, ball=3,
                  batsman=batsmen[0], bowler=bowlers[2],
                  runs_batsman=4, runs_total=4, is_boundary=True),
        BallEvent(match_id="sim_001", innings=1, over=2, ball=4,
                  batsman=batsmen[0], bowler=bowlers[2],
                  runs_batsman=0, runs_total=0),
        BallEvent(match_id="sim_001", innings=1, over=2, ball=5,
                  batsman=batsmen[0], bowler=bowlers[2],
                  runs_batsman=2, runs_total=2),
        BallEvent(match_id="sim_001", innings=1, over=2, ball=6,
                  batsman=batsmen[0], bowler=bowlers[2],
                  runs_batsman=1, runs_total=1),
    ]

    return match_state, balls


async def run_replay(
    match_id: str | None = None,
    simulate: bool = False,
    delay: float = 2.0,
) -> None:
    """Run the replay pipeline."""
    factory = ProviderFactory()

    if simulate:
        logger.info("Running in simulation mode (no API key needed)")
        match_state, balls = create_simulated_match()
    else:
        if not match_id:
            logger.error("Either --match-id or --simulate is required")
            return

        logger.info("Fetching match data for %s from API...", match_id)
        data_feed = factory.create_data_feed()
        match_state = await data_feed.get_match_scorecard(match_id)
        balls = await data_feed.get_ball_by_ball(match_id)

    logger.info(
        "Match: %s | %d balls to process | delay=%.1fs between balls",
        match_state.match_info.title,
        len(balls),
        delay,
    )

    # Create the pipeline
    pipeline = factory.create_pipeline()

    # Run replay
    audio_chunks = await pipeline.process_replay(
        match_id=match_state.match_info.match_id,
        balls=balls,
        state=match_state,
        delay_between_balls=delay,
    )

    # Summary
    logger.info("=" * 60)
    logger.info("REPLAY COMPLETE")
    logger.info("Balls processed: %d", len(balls))
    logger.info("Audio chunks generated: %d", len(audio_chunks))
    total_duration_ms = sum(c.duration_ms for c in audio_chunks)
    logger.info("Total audio duration: %.1f seconds", total_duration_ms / 1000)
    total_cost = sum(c.tts_cost_usd for c in audio_chunks) + sum(
        c.commentary.llm_cost_usd for c in audio_chunks
    )
    logger.info("Total cost: $%.6f", total_cost)
    logger.info("Audio files at: output/audio/")
    logger.info("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Replay a cricket match through the pipeline")
    parser.add_argument("--match-id", help="Match ID from the data provider")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Use simulated match data (no API key needed)",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=2.0,
        help="Delay between balls in seconds (default: 2.0)",
    )

    args = parser.parse_args()

    if not args.match_id and not args.simulate:
        parser.error("Either --match-id or --simulate is required")

    asyncio.run(run_replay(
        match_id=args.match_id,
        simulate=args.simulate,
        delay=args.delay,
    ))


if __name__ == "__main__":
    main()

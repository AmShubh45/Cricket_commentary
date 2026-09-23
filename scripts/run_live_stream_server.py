"""Live Broadcast Stream Server (100% Free / Zero API Keys / No Bedrock).

Serves a full 1080p live broadcast screen at http://localhost:8000/ with:
- Live TV-style scorecard updating in real time
- Real-time Hindi commentary ticker
- Automatic voice playback (via browser only — no desktop audio duplication)
- WebSocket push to OBS Studio / Browser Source

Usage:
    python scripts/run_live_stream_server.py
    Then open http://localhost:8000 in your browser or add it as a Browser Source in OBS!
    OBS: capture ONLY the browser source audio output to avoid double-playback.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

ROOT_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT_DIR))
sys.path.insert(0, str(ROOT_DIR / "src"))

# CRITICAL: Load .env BEFORE any pydantic-settings classes are instantiated
# Without this, TTSSettings/LLMSettings defaults (edge_tts, bedrock) are used
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT_DIR / ".env", override=True)
except ImportError:
    pass  # dotenv not installed — will rely on pydantic-settings env_file


from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

from config.settings import get_settings
from cricket.domain.enums import EmotionTag, EventTier, EventType, MatchFormat, MatchStatus
from cricket.domain.models import CommentaryLine
from cricket.infra.factory import ProviderFactory
from cricket.providers.data_feed.cricbuzz import CricbuzzProvider
from cricket.providers.llm.free_llm import FreeLLMProvider

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("LiveStreamServer")

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    match_id = os.getenv("CRICKET_MATCH_ID", "163061")
    poll_sec = int(os.getenv("CRICKET_POLL_INTERVAL", "8"))
    poller_task = asyncio.create_task(live_cricket_poller(match_id=match_id, poll_interval=poll_sec))
    yield
    poller_task.cancel()

app = FastAPI(title="Cricket Live Stream Server", lifespan=lifespan)

# Mount static files and audio outputs
output_dir = ROOT_DIR / "output" / "live"
output_dir.mkdir(parents=True, exist_ok=True)
templates_dir = ROOT_DIR / "templates" / "scorecard"

app.mount("/static", StaticFiles(directory=str(templates_dir)), name="static")
app.mount("/audio", StaticFiles(directory=str(output_dir)), name="audio")

# --- Broadcast Settings (runtime-configurable via UI sidebar) ---
broadcast_settings: dict[str, str] = {
    "commentary_language": "hindi",
    "commentator_style": "default",
    "tts_provider": os.getenv("TTS_PROVIDER", "edge_tts"),
    "tts_voice": os.getenv("TTS_VOICE_ID", "hi-IN-MadhurNeural"),
}
_active_tts_provider = None


@app.get("/api/settings")
async def get_broadcast_settings():
    """Return current broadcast settings."""
    return broadcast_settings


@app.post("/api/settings")
async def update_broadcast_settings(request: Request):
    """Update broadcast settings from UI sidebar."""
    global _active_tts_provider
    data = await request.json()
    old_tts = broadcast_settings.get("tts_provider")
    old_voice = broadcast_settings.get("tts_voice")
    broadcast_settings.update(data)

    # Hot-swap TTS provider if changed
    new_tts = broadcast_settings.get("tts_provider")
    new_voice = broadcast_settings.get("tts_voice")
    if new_tts != old_tts or new_voice != old_voice:
        try:
            from cricket.providers.tts.edge_tts_provider import EdgeTTSProvider
            from cricket.providers.tts.sarvam_tts_provider import SarvamTTSProvider

            if new_tts == "sarvam":
                _active_tts_provider = SarvamTTSProvider(get_settings().tts)
            elif new_tts == "google_cloud":
                from cricket.providers.tts.google_cloud_tts_provider import GoogleCloudTTSProvider
                _active_tts_provider = GoogleCloudTTSProvider(get_settings().tts, voice=new_voice)
            else:
                _active_tts_provider = EdgeTTSProvider(get_settings().tts, voice=new_voice)
            logger.info("\U0001f504 Hot-swapped TTS: %s (voice: %s)", new_tts, new_voice)
        except Exception as e:
            logger.error("Failed to hot-swap TTS: %s", e)

    logger.info("\u2699\ufe0f Settings updated: %s", broadcast_settings)
    return broadcast_settings

import time
from dataclasses import dataclass

@dataclass
class CommentaryTask:
    """A task representing a commentary line to be synthesized and broadcasted in strict FIFO order."""
    kind: str           # "ball", "over", "break", "ambient", "preview", "session"
    generate_fn: Any    # async callable returning CommentaryLine
    event_label: str
    event_type: str
    scorecard_payload: dict[str, Any]
    recent_pills: list[dict[str, Any]]
    created_at: float
    item_key: str = ""


class ConnectionManager:
    """Manages active WebSocket connections to broadcast live updates."""

    def __init__(self) -> None:
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.active_connections.append(websocket)
        logger.info("New broadcast screen connected (%d total)", len(self.active_connections))

    def disconnect(self, websocket: WebSocket) -> None:
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
            logger.info("Broadcast screen disconnected (%d remaining)", len(self.active_connections))

    async def broadcast(self, data: dict[str, Any]) -> None:
        message = json.dumps(data)
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message)
            except Exception:
                self.disconnect(connection)


manager = ConnectionManager()
current_match_state: dict[str, Any] = {}


@app.get("/", response_class=HTMLResponse)
async def get_live_screen() -> HTMLResponse:
    """Serve the full-screen broadcast interface."""
    live_html_path = templates_dir / "live.html"
    return HTMLResponse(content=live_html_path.read_text(encoding="utf-8"))


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    """WebSocket endpoint for real-time scorecard and commentary streaming."""
    await manager.connect(websocket)
    # Send initial state immediately
    if current_match_state:
        await websocket.send_text(json.dumps(current_match_state))
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


async def live_cricket_poller(match_id: str = "163061", poll_interval: int = 15) -> None:
    """Background task that continuously polls Cricbuzz and broadcasts updates.

    Key design decisions:
      - Scorecard is pushed to clients IMMEDIATELY when polled (before TTS).
      - TTS synthesis runs asynchronously via asyncio.create_task() so it never
        blocks the polling loop — reducing perceived latency by 2-4 seconds.
      - All balls pre-populated on startup to avoid replaying old commentary.
      - Stale balls (>60s old) are skipped even if unseen.
    """
    global current_match_state
    logger.info("Starting Cricbuzz live poller for match %s...", match_id)

    settings = get_settings()
    factory = ProviderFactory(settings)

    data_provider = CricbuzzProvider(default_match_id=match_id)
    llm_provider = FreeLLMProvider()
    tts_provider = factory.create_tts()

    processed_ball_keys: set[str] = set()
    known_batsmen: set[str] = set()
    last_announced_over: int = -1
    last_status: str = ""
    has_announced_preview: bool = False
    last_commentary_time: float = 0.0   # monotonic; tracks when last audio was queued
    last_break_announced: str = ""       # prevents repeating the same break commentary
    last_wicket_time: float = 0.0        # tracks when last wicket fell

    # Pre-populate ALL current balls so we only commentate on truly new deliveries.
    # Using ALL (not [:-1]) prevents replaying the last ball from a previous session.
    try:
        initial_balls = await data_provider.get_ball_by_ball(match_id)
        for b in initial_balls:  # ALL existing balls, not [:-1]
            processed_ball_keys.add(f"{b.innings}_{b.over}_{b.ball}")
            known_batsmen.add(b.batsman.name)
        card_init = await data_provider.get_match_scorecard(match_id)
        if card_init and card_init.get_current_innings():
            for b in card_init.get_current_innings().batsmen:
                known_batsmen.add(b.player.display_name)
        logger.info("Pre-populated %d ball keys (no replay of old balls)", len(processed_ball_keys))
    except Exception as e:
        logger.warning("Error getting initial ball feed: %s", e)

    # Dedicated FIFO Queue to guarantee 100% sequential commentary playback
    commentary_queue: asyncio.Queue[CommentaryTask] = asyncio.Queue()
    last_commentary_time = time.monotonic()

    async def commentary_worker() -> None:
        nonlocal last_commentary_time
        logger.info("Sequential commentary worker started (FIFO queue)")
        while True:
            task = await commentary_queue.get()
            try:
                # 1. Stale delivery drop (if game moved ahead by >30 seconds while audio was synthesizing)
                age = time.monotonic() - task.created_at
                if task.kind in ("ball", "ambient") and age > 30.0:
                    logger.info("⏩ Skipping stale %s commentary %s (age %.1fs)", task.kind, task.item_key, age)
                    continue

                # 2. Generate commentary text
                comm = await task.generate_fn()
                if not comm or not comm.text:
                    continue

                logger.info("🎤 Hindi Commentary [%s]: %s", task.event_label, comm.text)

                # 3. Synthesize audio sequentially via Sarvam
                safe_key = task.item_key.replace(".", "_") if task.item_key else f"{task.kind}_{int(time.time()*1000)}"
                audio_filename = f"{task.kind}_{safe_key}_{int(time.time())}.mp3"
                audio_path = output_dir / audio_filename

                active_tts = _active_tts_provider or tts_provider
                try:
                    await active_tts.synthesize(comm, str(audio_path))
                except Exception as tts_err:
                    logger.warning("⚠️ TTS failed (%s), falling back to Edge TTS: %s", type(active_tts).__name__, tts_err)
                    from cricket.providers.tts.edge_tts_provider import EdgeTTSProvider
                    fallback_tts = EdgeTTSProvider(get_settings().tts)
                    await fallback_tts.synthesize(comm, str(audio_path))
                audio_url = f"/audio/{audio_filename}"

                # 4. Broadcast in EXACT sequential order to the browser!
                comm_payload = {
                    "text": comm.text,
                    "event_label": task.event_label,
                    "event_type": task.event_type,
                }
                current_match_state["commentary"] = comm_payload
                current_match_state["audio_url"] = audio_url

                await manager.broadcast({
                    "scorecard": task.scorecard_payload,
                    "commentary": comm_payload,
                    "audio_url": audio_url,
                    "recent_balls": task.recent_pills,
                })
                last_commentary_time = time.monotonic()

            except Exception as exc:
                logger.error("Commentary worker error on %s: %s", task.kind, exc, exc_info=True)
            finally:
                commentary_queue.task_done()

    worker_task = asyncio.create_task(commentary_worker())

    while True:
        try:
            # Apply latest UI settings to LLM provider
            llm_provider.set_style(
                language=broadcast_settings.get("commentary_language", "hindi"),
                personality=broadcast_settings.get("commentator_style", "default"),
            )

            scorecard = await data_provider.get_match_scorecard(match_id)
            innings = scorecard.get_current_innings()
            balls = await data_provider.get_ball_by_ball(match_id)

            if innings:
                # Update known batsmen
                for b in innings.batsmen:
                    known_batsmen.add(b.player.display_name)

                # Build scorecard payload
                b1 = innings.batsmen[0] if (innings.batsmen and innings.batsmen[0].player.name) else None
                b2 = innings.batsmen[1] if (len(innings.batsmen) > 1 and innings.batsmen[1].player.name) else None
                bw = innings.bowler if (innings.bowler and innings.bowler.player.name) else None

                # Calculate two-over tracking (Previous Over & Current Over)
                overs_map: dict[int, list[Any]] = {}
                for ball in balls:
                    overs_map.setdefault(ball.over, []).append(ball)

                sorted_ov_nums = sorted(overs_map.keys())
                cur_ov_num = sorted_ov_nums[-1] if sorted_ov_nums else 0
                prev_ov_num = sorted_ov_nums[-2] if len(sorted_ov_nums) > 1 else None

                cur_balls = overs_map.get(cur_ov_num, [])
                cur_pills = []
                cur_over_runs = 0
                for b in cur_balls:
                    if b.wicket:
                        lbl, p_type = "W", "wicket"
                    elif b.is_six:
                        lbl, p_type = "6", "six"
                    elif b.is_boundary:
                        lbl, p_type = "4", "boundary"
                    elif b.is_wide:
                        lbl, p_type = "Wd", "extra"
                    elif b.is_no_ball:
                        lbl, p_type = "Nb", "extra"
                    elif b.runs_batsman > 0:
                        lbl, p_type = str(b.runs_batsman), "single"
                    else:
                        lbl, p_type = "0", "dot"
                    cur_pills.append({"label": lbl, "type": p_type, "runs": b.runs_total})
                    cur_over_runs += b.runs_total

                prev_balls = overs_map.get(prev_ov_num, []) if prev_ov_num is not None else []
                prev_pills = []
                prev_over_runs = 0
                for b in prev_balls:
                    if b.wicket:
                        lbl, p_type = "W", "wicket"
                    elif b.is_six:
                        lbl, p_type = "6", "six"
                    elif b.is_boundary:
                        lbl, p_type = "4", "boundary"
                    elif b.is_wide:
                        lbl, p_type = "Wd", "extra"
                    elif b.is_no_ball:
                        lbl, p_type = "Nb", "extra"
                    elif b.runs_batsman > 0:
                        lbl, p_type = str(b.runs_batsman), "single"
                    else:
                        lbl, p_type = "0", "dot"
                    prev_pills.append({"label": lbl, "type": p_type, "runs": b.runs_total})
                    prev_over_runs += b.runs_total

                two_overs = {
                    "prev_over": {
                        "over_num": prev_ov_num + 1 if prev_ov_num is not None else None,
                        "balls": prev_pills,
                        "runs": prev_over_runs,
                    },
                    "cur_over": {
                        "over_num": cur_ov_num + 1 if sorted_ov_nums else 1,
                        "balls": cur_pills,
                        "runs": cur_over_runs,
                        "balls_remaining": max(0, 6 - len(cur_pills)),
                    }
                }

                recent_pills = cur_pills

                # Calculate lead / trail or target equation
                inns_summary = data_provider.get_innings_summary()
                all_inns = inns_summary.get("all_innings", [])
                lead_trail_text = ""
                target_val = inns_summary.get("target", 0)
                rem_runs_val = inns_summary.get("rem_runs", 0)
                is_test_match = (scorecard.match_info.format == MatchFormat.TEST)

                if target_val > 0:
                    if rem_runs_val > 0:
                        lead_trail_text = f"{innings.batting_team.short_name} NEED {rem_runs_val} RUNS (TARGET {target_val})"
                    else:
                        lead_trail_text = f"TARGET {target_val}"
                elif is_test_match and all_inns:
                    # ONLY for Test matches: calculate lead / trail
                    bat_short = innings.batting_team.short_name
                    bowl_short = innings.bowling_team.short_name
                    bat_runs_tot = sum(i["score"] for i in all_inns if i["team_name"] == bat_short)
                    bowl_runs_tot = sum(i["score"] for i in all_inns if i["team_name"] == bowl_short)
                    diff = bat_runs_tot - bowl_runs_tot
                    if diff > 0:
                        lead_trail_text = f"{bat_short} LEAD BY {diff} RUNS"
                    elif diff < 0:
                        lead_trail_text = f"{bat_short} TRAIL BY {abs(diff)} RUNS"
                    else:
                        lead_trail_text = "SCORES ARE LEVEL"
                else:
                    # Limited overs 1st innings or break
                    status_lower = (scorecard.status_text or "").lower()
                    if "innings break" in status_lower or innings.total_wickets >= 10:
                        lead_trail_text = f"INNINGS BREAK — TARGET {innings.total_runs + 1}"
                    else:
                        lead_trail_text = f"1ST INNINGS — {scorecard.match_info.series_name.upper()}"

                # Upcoming batsman ("Coming")
                coming_name = "Next In"
                coming_id = ""
                if innings.total_wickets >= 10:
                    coming_name = "All Out"
                else:
                    for b in innings.batsmen:
                        if b1 and b.player.name == b1.player.name:
                            continue
                        if b2 and b.player.name == b2.player.name:
                            continue
                        coming_name = b.player.display_name
                        coming_id = b.player.id
                        break

                last_ball = balls[-1] if balls else None
                last_ball_outcome = {
                    "label": "W" if (last_ball and last_ball.wicket) else ("6" if (last_ball and last_ball.is_six) else ("4" if (last_ball and last_ball.is_boundary) else str(last_ball.runs_batsman if last_ball else "0"))),
                    "type": "wicket" if (last_ball and last_ball.wicket) else ("six" if (last_ball and last_ball.is_six) else ("boundary" if (last_ball and last_ball.is_boundary) else ("single" if (last_ball and last_ball.runs_batsman > 0) else "dot"))),
                    "runs": last_ball.runs_batsman if last_ball else 0,
                    "over_display": last_ball.over_display if last_ball else "",
                }

                just_lost_wicket = any(b.wicket for b in balls[-3:]) or (time.monotonic() - last_wicket_time < 180)
                partnership_display = "-" if just_lost_wicket else (f"{innings.partnership_runs} ({innings.partnership_balls})" if innings.partnership_balls > 0 else "-")

                scorecard_payload = {
                    "match_title": scorecard.match_info.title,
                    "series_name": scorecard.match_info.series_name,
                    "status_text": scorecard.status_text,
                    "is_live": scorecard.status == MatchStatus.LIVE,
                    "batting_team": innings.batting_team.short_name,
                    "bowling_team": innings.bowling_team.short_name,
                    "batting_team_name": innings.batting_team.name,
                    "bowling_team_name": innings.bowling_team.name,
                    "total_runs": innings.total_runs,
                    "total_wickets": innings.total_wickets,
                    "total_overs": innings.total_overs,
                    "current_rr": f"{innings.run_rate:.2f}" if innings.run_rate else "0.00",
                    "partnership": partnership_display,
                    "lead_trail_text": lead_trail_text,
                    "two_overs": two_overs,
                    "last_ball_outcome": last_ball_outcome,
                    "batsman1": {
                        "id": b1.player.id,
                        "name": b1.player.display_name,
                        "runs": b1.runs,
                        "balls": b1.balls_faced,
                        "sr": f"{b1.strike_rate:.1f}",
                        "fours": b1.fours,
                        "sixes": b1.sixes,
                        "is_on_strike": b1.is_on_strike,
                        "image_url": f"https://images.cricbuzz.com/images/player/{b1.player.id}.jpg" if b1.player.id else "",
                    } if b1 else None,
                    "batsman2": {
                        "id": b2.player.id,
                        "name": b2.player.display_name,
                        "runs": b2.runs,
                        "balls": b2.balls_faced,
                        "sr": f"{b2.strike_rate:.1f}",
                        "fours": b2.fours,
                        "sixes": b2.sixes,
                        "is_on_strike": b2.is_on_strike,
                        "image_url": f"https://images.cricbuzz.com/images/player/{b2.player.id}.jpg" if b2.player.id else "",
                    } if b2 else None,
                    "coming_batsman": {
                        "id": coming_id,
                        "name": coming_name,
                        "image_url": f"https://images.cricbuzz.com/images/player/{coming_id}.jpg" if coming_id else "",
                    },
                    "bowler": {
                        "id": bw.player.id,
                        "name": bw.player.display_name,
                        "wickets": bw.wickets,
                        "runs": bw.runs_conceded,
                        "overs": bw.overs,
                        "econ": f"{bw.economy:.2f}",
                        "image_url": f"https://images.cricbuzz.com/images/player/{bw.player.id}.jpg" if bw.player.id else "",
                    } if bw else None,
                    # All innings (crucial for Test matches showing previous innings)
                    "innings_summary": inns_summary,
                    "innings_number": innings.innings_number,
                    "match_format": scorecard.match_info.format.value,
                }

                current_match_state["scorecard"] = scorecard_payload
                current_match_state["recent_balls"] = recent_pills

                # ---- Detect match breaks: innings break / tea / lunch / stumps / rain / all out ----
                status_now = scorecard.status_text or ""
                break_keywords = ("tea", "lunch", "stumps", "rain", "bad light", "drinks", "delayed", "innings break", "break")
                is_innings_break = ("innings break" in status_now.lower()) or (innings.total_wickets >= 10 and innings.innings_number == 1)
                is_break = is_innings_break or any(kw in status_now.lower() for kw in break_keywords)

                if is_break and status_now != last_break_announced:
                    last_break_announced = status_now
                    logger.info("Break detected: %s (innings_break=%s) — enqueuing broadcast commentary", status_now, is_innings_break)

                    async def _gen_break(status=status_now, inn=innings, tgt=target_val) -> CommentaryLine:
                        return llm_provider.generate_break_commentary(
                            status_text=status,
                            team_name=inn.batting_team.name,
                            score=inn.total_runs,
                            wickets=inn.total_wickets,
                            overs=inn.total_overs,
                            run_rate=inn.run_rate,
                            target=tgt,
                            chasing_team=inn.bowling_team.name,
                        )

                    await commentary_queue.put(CommentaryTask(
                        kind="break",
                        generate_fn=_gen_break,
                        event_label="BREAK",
                        event_type="innings_break" if is_innings_break else "drinks_break",
                        scorecard_payload=scorecard_payload,
                        recent_pills=recent_pills,
                        created_at=time.monotonic(),
                        item_key="break",
                    ))

                # Handle UPCOMING match preview
                if scorecard.status == MatchStatus.UPCOMING:
                    if not has_announced_preview:
                        has_announced_preview = True
                        preview_text = f"Welcome to live coverage of {scorecard.match_info.series_name}! {scorecard.match_info.title}. {scorecard.status_text}. Live ball-by-ball commentary and visual stream will start as soon as the match begins!"
                        logger.info("🎤 Pre-match Preview: %s", preview_text)

                        async def _gen_preview(pt=preview_text) -> CommentaryLine:
                            return CommentaryLine(
                                text=pt,
                                emotion=EmotionTag.EXCITED,
                                event_type=EventType.DOT_BALL,
                                tier=EventTier.MAJOR,
                                source="preview",
                                match_id=match_id,
                                over_display="",
                                timestamp=datetime.now(UTC),
                            )

                        await commentary_queue.put(CommentaryTask(
                            kind="preview",
                            generate_fn=_gen_preview,
                            event_label="PREVIEW",
                            event_type="preview",
                            scorecard_payload=scorecard_payload,
                            recent_pills=recent_pills,
                            created_at=time.monotonic(),
                            item_key="preview",
                        ))
                    else:
                        await manager.broadcast({
                            "scorecard": scorecard_payload,
                            "recent_balls": recent_pills,
                        })

                # Only process balls within the last 60 seconds to prevent stale replays
                now_utc = datetime.now(UTC)
                for ball in balls:
                    ball_key = f"{ball.innings}_{ball.over}_{ball.ball}"
                    if ball_key in processed_ball_keys:
                        continue

                    # Skip stale balls (>60s old) — prevents flooding after reconnect
                    ball_age_sec = (now_utc - ball.timestamp).total_seconds()
                    if ball_age_sec > 60:
                        logger.debug("Skipping stale ball %s (age %.0fs)", ball_key, ball_age_sec)
                        processed_ball_keys.add(ball_key)
                        continue

                    processed_ball_keys.add(ball_key)

                    logger.info("⚡ Live Delivery: Over %s by %s to %s", ball.over_display, ball.bowler.name, ball.batsman.name)

                    # Classify event
                    event_type = EventType.DOT_BALL
                    emotion = EmotionTag.NEUTRAL
                    event_label = "DOT"
                    if ball.is_six:
                        event_type = EventType.SIX
                        emotion = EmotionTag.EXCITED
                        event_label = "SIX"
                    elif ball.is_boundary:
                        event_type = EventType.BOUNDARY
                        emotion = EmotionTag.EXCITED
                        event_label = "FOUR"
                    elif ball.wicket:
                        event_type = EventType.WICKET
                        emotion = EmotionTag.DRAMATIC
                        event_label = "WICKET"
                    elif ball.is_single:
                        event_type = EventType.SINGLE
                        event_label = "1 RUN"
                    elif ball.runs_batsman > 1:
                        event_type = EventType.DOUBLE
                        event_label = f"{ball.runs_batsman} RUNS"

                    # --- STEP 1: Push scorecard update IMMEDIATELY (don't wait for TTS) ---
                    comm_placeholder = {
                        "text": f"Over {ball.over_display} — {event_label} by {ball.batsman.name}",
                        "event_label": event_label,
                        "event_type": event_type.value,
                    }
                    current_match_state["commentary"] = comm_placeholder
                    await manager.broadcast({
                        "scorecard": scorecard_payload,
                        "commentary": comm_placeholder,
                        "recent_balls": recent_pills,
                        # No audio_url yet — browser shows updated scorecard immediately
                    })

                    # --- STEP 2: Enqueue ball commentary for strictly sequential synthesis ---
                    b1_runs_val = b1.runs if b1 else 0

                    async def _gen_ball_comm(
                        b=ball,
                        ev_t=event_type,
                        em=emotion,
                        inn=innings,
                        b1_r=b1_runs_val,
                    ) -> CommentaryLine:
                        if b.wicket:
                            nonlocal last_wicket_time
                            last_wicket_time = time.monotonic()
                            new_batter_name = None
                            for batter in inn.batsmen:
                                if batter.player.display_name != b.batsman.name and batter.player.display_name not in known_batsmen:
                                    new_batter_name = batter.player.display_name
                                    known_batsmen.add(new_batter_name)
                                    break

                            # Accurately extract total innings score and balls for out batsman
                            out_runs = 0
                            out_balls = 0
                            # 1. Parse score from commentary text e.g. "Harry Brook run out ... 75(97) [4s-6]"
                            m_comm = re.search(r'\b(\d+)\s*\(\s*(\d+)\s*\)', b.commentary_raw)
                            if m_comm:
                                out_runs = int(m_comm.group(1))
                                out_balls = int(m_comm.group(2))

                            # 2. Try lastWicket in scorecard / miniscore
                            if not out_runs and inn.last_wicket:
                                m_last = re.search(r'\b(\d+)\s*\(\s*(\d+)\s*\)', inn.last_wicket)
                                if m_last:
                                    out_runs = int(m_last.group(1))
                                    out_balls = int(m_last.group(2))

                            # 3. Match from active batsmen list if available
                            if not out_runs and inn.batsmen:
                                for batter in inn.batsmen:
                                    if batter.player.display_name == b.batsman.name or batter.player.name == b.batsman.name:
                                        if batter.runs > 0:
                                            out_runs = batter.runs
                                            out_balls = batter.balls_faced
                                        break

                            return llm_provider.generate_wicket_announcement(
                                out_batter=b.batsman.name,
                                runs=out_runs,
                                balls=out_balls,
                                dismissal_text=b.commentary_raw,
                                bowler=b.bowler.name,
                                new_batter=new_batter_name,
                                score=inn.total_runs,
                                wickets=inn.total_wickets,
                            )
                        else:
                            ctx = {
                                "match_id": match_id,
                                "over_display": b.over_display,
                                "bowler": b.bowler.name,
                                "batsman": b.batsman.name,
                                "runs_batsman": b.runs_batsman,
                                "batsman_runs": b1_r,
                                "team_score": inn.total_runs,
                                "team_wickets": inn.total_wickets,
                                "emotion": em.value,
                                "event_type": ev_t.value,
                            }
                            return await llm_provider.generate_commentary(b.commentary_raw, ctx)

                    await commentary_queue.put(CommentaryTask(
                        kind="ball",
                        generate_fn=_gen_ball_comm,
                        event_label=event_label,
                        event_type=event_type.value,
                        scorecard_payload=scorecard_payload,
                        recent_pills=recent_pills,
                        created_at=time.monotonic(),
                        item_key=ball.over_display,
                    ))

                    # --- STEP 3: Over summary strictly AFTER ball 6 ---
                    if ball.ball == 6 and ball.over != last_announced_over:
                        last_announced_over = ball.over
                        over_num = ball.over + 1
                        over_balls = [b for b in balls if b.over == ball.over]
                        runs_in_over = sum(b.runs_batsman for b in over_balls)
                        figures_str = f"{bw.wickets}/{bw.runs_conceded} ({bw.overs} ov)" if bw else ""

                        async def _gen_over_comm(
                            on=over_num,
                            r=runs_in_over,
                            bwn=ball.bowler.name,
                            fig=figures_str,
                            inn=innings,
                        ) -> CommentaryLine:
                            return llm_provider.generate_over_summary(
                                over_num=on,
                                runs_in_over=r,
                                bowler_name=bwn,
                                figures=fig,
                                team_name=inn.batting_team.name,
                                score=inn.total_runs,
                                wickets=inn.total_wickets,
                            )

                        await commentary_queue.put(CommentaryTask(
                            kind="over",
                            generate_fn=_gen_over_comm,
                            event_label="OVER",
                            event_type="over_end",
                            scorecard_payload=scorecard_payload,
                            recent_pills=recent_pills,
                            created_at=time.monotonic(),
                            item_key=f"over_{over_num}",
                        ))

                # 3. Check for Stumps / Session transition
                status_raw = str(scorecard.status.value)
                if status_raw and status_raw != last_status and ("stumps" in status_raw.lower() or "tea" in status_raw.lower() or "lunch" in status_raw.lower()):
                    last_status = status_raw
                    async def _gen_session(s_raw=status_raw, inn=innings) -> CommentaryLine:
                        return llm_provider.generate_session_summary(
                            status_text=s_raw,
                            team_name=inn.batting_team.name,
                            score=inn.total_runs,
                            wickets=inn.total_wickets,
                            overs=inn.total_overs,
                        )
                    await commentary_queue.put(CommentaryTask(
                        kind="session",
                        generate_fn=_gen_session,
                        event_label="SESSION",
                        event_type="innings_break",
                        scorecard_payload=scorecard_payload,
                        recent_pills=recent_pills,
                        created_at=time.monotonic(),
                        item_key="session",
                    ))

            # ---- Ambient commentary: ONLY when queue is completely empty and >18s of silence and NOT in a break and NOT all-out ----
            elapsed = time.monotonic() - last_commentary_time
            if elapsed > 18.0 and commentary_queue.empty() and innings and not is_break and innings.total_wickets < 10:
                async def _gen_ambient(inn=innings) -> CommentaryLine:
                    b1n = inn.batsmen[0].player.display_name if inn.batsmen else ""
                    b1_r = inn.batsmen[0].runs if inn.batsmen else 0
                    b2n = inn.batsmen[1].player.display_name if len(inn.batsmen) > 1 else ""
                    b2_r = inn.batsmen[1].runs if len(inn.batsmen) > 1 else 0
                    bwn = inn.bowler.player.display_name if inn.bowler else ""
                    bw_figs = f"{inn.bowler.wickets}/{inn.bowler.runs_conceded} ({inn.bowler.overs} ov)" if inn.bowler else ""
                    bw_eco = f"{inn.bowler.economy:.2f}" if inn.bowler else ""
                    pship_str = f"{inn.partnership_runs} runs ({inn.partnership_balls} balls)" if inn.partnership_balls > 0 else ""
                    last_ov_str = f"Over {two_overs['cur_over']['over_num']}: {two_overs['cur_over']['runs']} runs" if sorted_ov_nums else ""

                    just_wkt = any(b.wicket for b in balls[-3:]) or (time.monotonic() - last_wicket_time < 180)
                    amb_ctx = {
                        "team_name": inn.batting_team.name,
                        "score": inn.total_runs,
                        "wickets": inn.total_wickets,
                        "overs": inn.total_overs,
                        "run_rate": inn.run_rate,
                        "lead_trail_or_target": lead_trail_text,
                        "partnership": "" if just_wkt else pship_str,
                        "just_lost_wicket": just_wkt,
                        "batsman1": b1n,
                        "striker_name": b1n,
                        "striker_runs": b1_r,
                        "batsman2": b2n if not just_wkt else "",
                        "non_striker_name": b2n if not just_wkt else "",
                        "non_striker_runs": b2_r if not just_wkt else 0,
                        "bowler": bwn,
                        "bowler_name": bwn,
                        "bowler_figures": bw_figs,
                        "bowler_econ": bw_eco,
                        "last_over_info": last_ov_str,
                    }
                    comm = await llm_provider.generate_ambient_llm(amb_ctx)
                    if not comm:
                        comm = llm_provider.generate_ambient_commentary(
                            batsman1=b1n,
                            batsman2=b2n if not just_wkt else "",
                            bowler=bwn,
                            score=inn.total_runs,
                            wickets=inn.total_wickets,
                            overs=inn.total_overs,
                            run_rate=inn.run_rate,
                            partnership_runs=0 if just_wkt else inn.partnership_runs,
                            partnership_balls=0 if just_wkt else inn.partnership_balls,
                            lead_trail_or_target=lead_trail_text,
                            striker_runs=b1_r,
                            bowler_figures=bw_figs,
                            last_over_info=last_ov_str,
                            just_lost_wicket=just_wkt,
                        )
                    return comm

                logger.info("🎙️ Enqueuing ambient commentary (silence was %.0fs)", elapsed)
                await commentary_queue.put(CommentaryTask(
                    kind="ambient",
                    generate_fn=_gen_ambient,
                    event_label="LIVE",
                    event_type="dot_ball",
                    scorecard_payload=scorecard_payload,
                    recent_pills=recent_pills,
                    created_at=time.monotonic(),
                    item_key="ambient",
                ))

            # Clean up old audio files (>5 minutes) to prevent disk exhaustion
            try:
                cutoff = time.time() - 300
                for f in output_dir.glob("*.mp3"):
                    if f.stat().st_mtime < cutoff:
                        f.unlink(missing_ok=True)
            except Exception:
                pass

            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            worker_task.cancel()
            break
        except Exception as e:
            logger.error("Error in live polling loop: %s", e, exc_info=True)
            await asyncio.sleep(poll_interval)
    worker_task.cancel()



def main() -> None:
    parser = argparse.ArgumentParser(description="Cricket Live Stream Server (Free)")
    parser.add_argument("--match-id", default="163061", help="Cricbuzz Match ID (default: 163061)")
    parser.add_argument("--poll-interval", type=int, default=15, help="Poll interval in seconds (default: 15)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)), help="Port to bind server (default: 8000 or $PORT)")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface (default: 0.0.0.0)")

    args = parser.parse_args()

    os.environ["CRICKET_MATCH_ID"] = args.match_id
    os.environ["CRICKET_POLL_INTERVAL"] = str(args.poll_interval)

    print("\n" + "=" * 75)
    print(f"📡 CRICKET LIVE BROADCAST STREAM SERVER RUNNING")
    print(f"   Match ID: {args.match_id}")
    print(f"   Screen URL: http://localhost:{args.port}/")
    print(f"   OBS Browser Source: http://localhost:{args.port}/ (1920x1080)")
    print("=" * 75 + "\n")

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

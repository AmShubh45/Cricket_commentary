"""Cricbuzz live cricket data provider.

Scrapes and decodes real-time ball-by-ball commentary and live scorecards
directly from Cricbuzz Next.js streaming HTML.
Requires NO paid API key or subscription.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from cricket.domain.enums import (
    MatchFormat,
    MatchStatus,
    WicketType,
)
from cricket.domain.models import (
    BallEvent,
    BatsmanState,
    BowlerState,
    InningsState,
    MatchInfo,
    MatchState,
    PlayerInfo,
    TeamInfo,
    WicketInfo,
)

logger = logging.getLogger(__name__)

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


# Cache TTL in seconds — reuse the same parsed page within one polling cycle
_CACHE_TTL_SECONDS = 12


def _safe_int(val: Any, default: int = 0) -> int:
    """Parse integer safely handling '$undefined', None, and empty strings."""
    if val is None or val == "$undefined" or val == "":
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Parse float safely handling '$undefined', None, and empty strings."""
    if val is None or val == "$undefined" or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


class CricbuzzProvider:
    """Live cricket data provider reading directly from Cricbuzz web stream."""

    def __init__(self, default_match_id: str = "163061") -> None:
        self.default_match_id = default_match_id
        self._client = httpx.AsyncClient(
            headers=_DEFAULT_HEADERS,
            timeout=httpx.Timeout(15.0, connect=10.0),
            follow_redirects=True,
        )
        # Per-match page data cache: {match_id: (timestamp, data)}
        self._page_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def _fetch_page_data(self, match_id: str) -> dict[str, Any]:
        """Fetch and extract commentaryPageData from Cricbuzz HTML.

        Results are cached for _CACHE_TTL_SECONDS so that get_match_scorecard()
        and get_ball_by_ball() called in the same polling cycle share one HTTP
        round-trip instead of making two separate requests.
        """
        now = time.monotonic()
        cached = self._page_cache.get(match_id)
        if cached is not None:
            ts, data = cached
            if now - ts < _CACHE_TTL_SECONDS:
                logger.debug("CricbuzzProvider: cache hit for match %s (age %.1fs)", match_id, now - ts)
                return data

        url = f"https://www.cricbuzz.com/live-cricket-scores/{match_id}"
        resp = await self._client.get(url)
        resp.raise_for_status()
        html = resp.text

        page_data: dict[str, Any] = {}

        # Next.js App router embeds streaming chunks in self.__next_f.push([1, "..."])
        for match in re.finditer(r'self\.__next_f\.push\(\[1,\s*"(.*?)"\]\)', html, re.DOTALL):
            chunk_raw = match.group(1)
            if "commentaryPageData" in chunk_raw:
                # Unescape outer JSON string safely
                try:
                    unescaped = json.loads('"' + chunk_raw + '"')
                    idx = unescaped.find('{"commentaryPageData"')
                    if idx != -1:
                        decoder = json.JSONDecoder()
                        payload, _ = decoder.raw_decode(unescaped[idx:])
                        page_data = payload.get("commentaryPageData", {})
                        break
                except Exception as e:
                    logger.warning("Failed to decode Next.js chunk: %s", e)

        if not page_data:
            # Fallback: regex search for commentaryPageData directly
            m = re.search(r'\{[^{}]*?"commentaryPageData":\{.*?\}\}', html)
            if m:
                try:
                    decoder = json.JSONDecoder()
                    payload, _ = decoder.raw_decode(m.group(0))
                    page_data = payload.get("commentaryPageData", {})
                except Exception:
                    pass

        if not page_data:
            logger.warning("Could not find commentaryPageData in Cricbuzz page for match %s", match_id)

        # Store in cache
        self._page_cache[match_id] = (now, page_data)
        return page_data

    def invalidate_cache(self, match_id: str | None = None) -> None:
        """Force a fresh fetch on the next call (call after a known state change)."""
        if match_id is None:
            self._page_cache.clear()
        else:
            self._page_cache.pop(match_id, None)

    async def get_live_matches(self) -> list[MatchInfo]:
        """Return match info for the configured match."""
        card = await self.get_match_scorecard(self.default_match_id)
        return [card.match_info]

    async def get_match_scorecard(self, match_id: str) -> MatchState:
        """Fetch the full current scorecard and match state (handles upcoming & live)."""
        page_data = await self._fetch_page_data(match_id)
        miniscore = page_data.get("miniscore") or {}
        match_header = page_data.get("matchHeader") or {}

        # Team names and info from matchHeader or defaults
        team1_raw = match_header.get("team1") or {}
        team2_raw = match_header.get("team2") or {}
        t1_id = _safe_int(team1_raw.get("id"))
        t2_id = _safe_int(team2_raw.get("id"))
        t1_name = team1_raw.get("name") or "Team 1"
        t2_name = team2_raw.get("name") or "Team 2"
        t1_short = team1_raw.get("shortName") or t1_name[:4].upper()
        t2_short = team2_raw.get("shortName") or t2_name[:4].upper()

        bat_team_data = miniscore.get("batTeam") or {}
        bat_team_id = _safe_int(bat_team_data.get("teamId"))
        bat_name_candidate = bat_team_data.get("teamName")

        # Determine which team is batting dynamically based on teamId or teamName
        if (bat_team_id and bat_team_id == t2_id) or (bat_name_candidate and bat_name_candidate == t2_name):
            bat_team_name = t2_name
            bat_short = t2_short
            bowl_team_name = t1_name
            bowl_short = t1_short
        else:
            bat_team_name = t1_name
            bat_short = t1_short
            bowl_team_name = t2_name
            bowl_short = t2_short

        team_bat = TeamInfo(id="bat", name=bat_team_name, short_name=bat_short)
        team_bowl = TeamInfo(id="bowl", name=bowl_team_name, short_name=bowl_short)

        # Match Info
        fmt_str = str(match_header.get("matchFormat", "T20")).lower()
        match_fmt = MatchFormat.T20
        if "test" in fmt_str:
            match_fmt = MatchFormat.TEST
        elif "odi" in fmt_str:
            match_fmt = MatchFormat.ODI

        series_name = match_header.get("seriesName") or "Cricket Series"
        match_desc = match_header.get("matchDescription") or "Match"

        match_info = MatchInfo(
            match_id=match_id,
            title=f"{t1_name} vs {t2_name}, {match_desc}",
            format=match_fmt,
            venue=match_header.get("venueName") or "Cricket Ground",
            city=match_header.get("cityName") or "",
            team_home=TeamInfo(id="t1", name=t1_name, short_name=t1_short),
            team_away=TeamInfo(id="t2", name=t2_name, short_name=t2_short),
            series_name=series_name,
        )

        # Batsmen
        batsmen_list: list[BatsmanState] = []
        striker_raw = miniscore.get("batsmanStriker") or {}
        non_striker_raw = miniscore.get("batsmanNonStriker") or {}

        if striker_raw and striker_raw.get("name"):
            batsmen_list.append(
                BatsmanState(
                    player=PlayerInfo(id=str(striker_raw.get("id", "")), name=striker_raw.get("name", "")),
                    runs=_safe_int(striker_raw.get("runs", 0)),
                    balls_faced=_safe_int(striker_raw.get("balls", 0)),
                    fours=_safe_int(striker_raw.get("fours", 0)),
                    sixes=_safe_int(striker_raw.get("sixes", 0)),
                    is_on_strike=True,
                )
            )

        if non_striker_raw and non_striker_raw.get("name"):
            batsmen_list.append(
                BatsmanState(
                    player=PlayerInfo(id=str(non_striker_raw.get("id", "")), name=non_striker_raw.get("name", "")),
                    runs=_safe_int(non_striker_raw.get("runs", 0)),
                    balls_faced=_safe_int(non_striker_raw.get("balls", 0)),
                    fours=_safe_int(non_striker_raw.get("fours", 0)),
                    sixes=_safe_int(non_striker_raw.get("sixes", 0)),
                    is_on_strike=False,
                )
            )

        # Bowler
        bowler_state: BowlerState | None = None
        bowler_raw = miniscore.get("bowlerStriker") or {}
        if bowler_raw and bowler_raw.get("name"):
            bowler_state = BowlerState(
                player=PlayerInfo(id=str(bowler_raw.get("id", "")), name=bowler_raw.get("name", "")),
                overs=_safe_float(bowler_raw.get("overs", 0.0)),
                maidens=_safe_int(bowler_raw.get("maidens", 0)),
                runs_conceded=_safe_int(bowler_raw.get("runs", 0)),
                wickets=_safe_int(bowler_raw.get("wickets", 0)),
            )

        # Innings
        pship = miniscore.get("partnerShip") or {}
        has_started = bool(miniscore and bat_team_data)

        innings = InningsState(
            innings_number=_safe_int(miniscore.get("inningsId", 1), 1),
            batting_team=team_bat,
            bowling_team=team_bowl,
            total_runs=_safe_int(bat_team_data.get("teamScore", 0)),
            total_wickets=_safe_int(bat_team_data.get("teamWkts", 0)),
            total_overs=_safe_float(miniscore.get("overs", 0.0)),
            run_rate=_safe_float(miniscore.get("currentRunRate", 0.0)),
            batsmen=batsmen_list,
            bowler=bowler_state,
            last_wicket=str(miniscore.get("lastWicket", "") or ""),
            partnership_runs=_safe_int(pship.get("runs", 0)),
            partnership_balls=_safe_int(pship.get("balls", 0)),
        )

        # --- All innings scores (for Test multi-innings display) ---
        score_details = miniscore.get("matchScoreDetails") or {}
        innings_score_list = score_details.get("inningsScoreList") or []
        all_innings_scores = []
        for inn_s in innings_score_list:
            all_innings_scores.append({
                "innings_id":  _safe_int(inn_s.get("inningsId", 1), 1),
                "team_name":   inn_s.get("batTeamName", ""),
                "score":       _safe_int(inn_s.get("score", 0)),
                "wickets":     _safe_int(inn_s.get("wickets", 0)),
                "overs":       _safe_float(inn_s.get("overs", 0.0)),
                "is_declared": bool(inn_s.get("isDeclared", False)),
                "is_follow_on":bool(inn_s.get("isFollowOn", False)),
            })

        # Bowling team previous innings from bowlTeamScoreObj
        bowl_score_obj = miniscore.get("bowlTeamScoreObj") or {}
        bowl_team_innings = bowl_score_obj.get("teamInningsArray") or []

        target = _safe_int(miniscore.get("target", 0))
        required_rate = _safe_float(miniscore.get("requiredRunRate", 0.0))
        rem_runs = _safe_int(miniscore.get("remRunsToWin", 0))
        custom_status = score_details.get("customStatus", "") or ""

        match_status = MatchStatus.LIVE if has_started else MatchStatus.UPCOMING
        status_text = custom_status or match_header.get("status") or match_header.get("state") or ""
        if not has_started and match_header.get("matchStartTimeIST"):
            status_text = f"Starts at {match_header.get('matchStartTimeIST')} IST"
        elif not status_text:
            status_text = "LIVE" if has_started else "UPCOMING"

        # Cache extra data for get_innings_summary() to use without extra HTTP call
        self._last_all_innings: list[dict] = all_innings_scores
        self._last_target: int = target
        self._last_rem_runs: int = rem_runs
        self._last_required_rate: float = required_rate

        return MatchState(
            match_info=match_info,
            status=match_status,
            status_text=status_text,
            current_innings=innings.innings_number,
            innings=[innings],
            last_updated=datetime.now(UTC),
        )

    def get_innings_summary(self) -> dict:
        """Return all innings scores + target info from the last scorecard fetch.

        Call after get_match_scorecard() within the same polling cycle.
        Returns empty dict if scorecard hasn't been fetched yet.
        """
        return {
            "all_innings": getattr(self, "_last_all_innings", []),
            "target":       getattr(self, "_last_target", 0),
            "rem_runs":     getattr(self, "_last_rem_runs", 0),
            "required_rate":getattr(self, "_last_required_rate", 0.0),
        }

    async def get_ball_by_ball(self, match_id: str) -> list[BallEvent]:
        """Fetch recent ball deliveries in chronological order."""
        page_data = await self._fetch_page_data(match_id)
        match_comm = page_data.get("matchCommentary", {}) or {}

        if not match_comm:
            return []

        # Sort balls strictly chronologically by (inningsId, ballMetric, timestamp)
        def _ball_sort_key(b: dict[str, Any]) -> tuple[int, float, int]:
            inn = _safe_int(b.get("inningsId"), 1)
            metric = _safe_float(b.get("ballMetric"), 0.0)
            ts = _safe_int(b.get("timestamp"), 0)
            return (inn, metric, ts)

        # Filter out non-ball commentary items (e.g. preview text, toss cards, ad cards)
        valid_balls = [
            b for b in match_comm.values()
            if isinstance(b, dict) and b.get("ballMetric") not in (None, "$undefined", "")
        ]
        raw_balls = sorted(valid_balls, key=_ball_sort_key)
        ball_events: list[BallEvent] = []

        for b in raw_balls:
            ball_metric = b.get("ballMetric")
            # ballMetric can be float/int like 153.5 or string "$undefined"
            if ball_metric is None or ball_metric == "$undefined":
                continue

            try:
                metric_float = float(ball_metric)
            except (ValueError, TypeError):
                continue

            over_num = int(metric_float)
            ball_num = int(round((metric_float - over_num) * 10))

            comm_text = b.get("commText", "") or ""
            events = b.get("event", []) or []
            if isinstance(events, str):
                events = [events]
            # Normalize to lowercase set for reliable lookup
            events_lower = {str(e).lower().strip() for e in events}

            # --- Robust boundary/six detection ---
            # Prefer explicit event tags over substring text matching
            is_four = "four" in events_lower
            is_six = "six" in events_lower

            # --- Robust wicket detection ---
            # Only trigger on explicit Cricbuzz wicket events — NOT substrings like
            # "outside edge", "outstanding", "bowled on good length", etc.
            # Cricbuzz uses events like ["wicket"], ["WICKET"], or sets isWicket=1
            _WICKET_EVENTS = {"wicket", "out", "w"}
            is_wicket = bool(_WICKET_EVENTS & events_lower)
            # Secondary check: Cricbuzz sometimes sets an isWicket/dismissal field
            if not is_wicket:
                is_wicket = bool(
                    b.get("isWicket") or
                    b.get("dismissal") or
                    b.get("wicketCode")
                )

            # --- Accurate run detection ---
            # Prefer structured numeric field; fall back to comm text only as last resort
            runs_raw = b.get("batsmanRuns") or b.get("runs")
            if runs_raw is not None:
                try:
                    runs = int(runs_raw)
                except (ValueError, TypeError):
                    runs = 0
            elif is_six:
                runs = 6
            elif is_four:
                runs = 4
            else:
                # Text-based fallback — use whole-word patterns to avoid false matches
                runs = 0
                comm_lower = comm_text.lower()
                if re.search(r'\b1\s*run\b|\bsingle\b', comm_lower):
                    runs = 1
                elif re.search(r'\b2\s*runs\b|\bdouble\b', comm_lower):
                    runs = 2
                elif re.search(r'\b3\s*runs\b|\btriple\b', comm_lower):
                    runs = 3

            # Players
            batsman_raw = b.get("batsmanDetails") or {}
            bowler_raw = b.get("bowlerDetails") or {}

            batsman = PlayerInfo(
                id=str(batsman_raw.get("playerId", "")),
                name=batsman_raw.get("playerName", "Batsman"),
            )
            bowler = PlayerInfo(
                id=str(bowler_raw.get("playerId", "")),
                name=bowler_raw.get("playerName", "Bowler"),
            )

            # Wicket info — with accurate dismissal type detection
            wicket_info: WicketInfo | None = None
            if is_wicket:
                comm_lower = comm_text.lower()
                # Detect specific dismissal types using word boundaries
                if re.search(r'\bstumped\b', comm_lower):
                    wkt_type = WicketType.STUMPED
                elif re.search(r'\brun\s*out\b', comm_lower):
                    wkt_type = WicketType.RUN_OUT
                elif re.search(r'\blbw\b|\bleg before\b', comm_lower):
                    wkt_type = WicketType.LBW
                elif re.search(r'\bc\s*&\s*b\b|caught\s+and\s+bowled\b', comm_lower):
                    wkt_type = WicketType.CAUGHT_AND_BOWLED
                elif re.search(r'\bcaught\b|\bc\s+\w', comm_lower):
                    wkt_type = WicketType.CAUGHT
                elif re.search(r'\bbowled\b', comm_lower):
                    wkt_type = WicketType.BOWLED
                elif re.search(r'\bhit\s*wicket\b', comm_lower):
                    wkt_type = WicketType.HIT_WICKET
                else:
                    wkt_type = WicketType.BOWLED  # safe default

                wicket_info = WicketInfo(
                    wicket_type=wkt_type,
                    batsman_out=batsman,
                    bowler=bowler,
                )

            ball_events.append(
                BallEvent(
                    match_id=match_id,
                    innings=int(b.get("inningsId", 1) or 1),
                    over=over_num,
                    ball=ball_num,
                    batsman=batsman,
                    bowler=bowler,
                    runs_batsman=runs,
                    runs_total=runs,
                    is_boundary=is_four,
                    is_six=is_six,
                    is_wide="wide" in comm_text.lower(),
                    is_no_ball="no ball" in comm_text.lower(),
                    wicket=wicket_info,
                    commentary_raw=comm_text,
                    raw_data=b,
                    timestamp=datetime.fromtimestamp(b.get("timestamp", 0) / 1000.0, tz=UTC)
                    if b.get("timestamp")
                    else datetime.now(UTC),
                )
            )

        return ball_events

    async def healthcheck(self) -> bool:
        """Verify Cricbuzz connectivity."""
        try:
            resp = await self._client.get(f"https://www.cricbuzz.com/live-cricket-scores/{self.default_match_id}")
            return resp.status_code == 200
        except Exception:
            return False

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

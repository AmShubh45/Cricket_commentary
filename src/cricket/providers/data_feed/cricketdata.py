"""CricketData.org API provider implementation.

Maps the CricketData.org REST API (https://cricapi.com) to our DataFeedProvider
interface. Handles JSON parsing, field mapping, and rate limiting.

API docs: https://cricapi.com/how-to-use
Pricing: $5.99/mo (S plan, 2,000 hits/day) — our budget default.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config.settings import DataFeedSettings
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

# Mapping from CricketData API match type strings to our MatchFormat enum
_FORMAT_MAP: dict[str, MatchFormat] = {
    "t20": MatchFormat.T20,
    "t20i": MatchFormat.T20,
    "odi": MatchFormat.ODI,
    "test": MatchFormat.TEST,
    "t10": MatchFormat.T10,
}

_STATUS_MAP: dict[str, MatchStatus] = {
    "": MatchStatus.UPCOMING,
    "Match not started": MatchStatus.UPCOMING,
    "Live": MatchStatus.LIVE,
    "In Progress": MatchStatus.LIVE,
    "Match over": MatchStatus.COMPLETED,
    "Complete": MatchStatus.COMPLETED,
    "Abandoned": MatchStatus.ABANDONED,
    "No Result": MatchStatus.NO_RESULT,
}

_WICKET_TYPE_MAP: dict[str, WicketType] = {
    "bowled": WicketType.BOWLED,
    "caught": WicketType.CAUGHT,
    "caught behind": WicketType.CAUGHT_BEHIND,
    "c & b": WicketType.CAUGHT_AND_BOWLED,
    "lbw": WicketType.LBW,
    "stumped": WicketType.STUMPED,
    "run out": WicketType.RUN_OUT,
    "hit wicket": WicketType.HIT_WICKET,
    "retired hurt": WicketType.RETIRED_HURT,
    "retired out": WicketType.RETIRED_OUT,
}


class CricketDataProvider:
    """CricketData.org API client implementing the DataFeedProvider interface.

    Usage:
        provider = CricketDataProvider(settings)
        matches = await provider.get_live_matches()
        scorecard = await provider.get_match_scorecard(match_id)
    """

    def __init__(self, settings: DataFeedSettings) -> None:
        self._api_key = settings.cricketdata_api_key
        self._base_url = settings.cricketdata_base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"Accept": "application/json"},
        )

    async def _request(self, endpoint: str, params: dict[str, Any] | None = None) -> dict:
        """Make an authenticated API request with retry logic."""
        merged_params = {"apikey": self._api_key}
        if params:
            merged_params.update(params)

        url = f"{self._base_url}/{endpoint.lstrip('/')}"

        response = await self._do_request(url, merged_params)
        data = response.json()

        if data.get("status") != "success":
            msg = f"CricketData API error: {data.get('status', 'unknown')} — {data.get('info', '')}"
            logger.error(msg)
            raise CricketDataAPIError(msg)

        return data

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError, httpx.TimeoutException)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        before_sleep=lambda retry_state: logger.warning(
            "CricketData API retry %d/%d",
            retry_state.attempt_number,
            3,
        ),
    )
    async def _do_request(self, url: str, params: dict) -> httpx.Response:
        """Execute HTTP request with retry on network errors."""
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        return response

    # =========================================================================
    # DataFeedProvider Interface Implementation
    # =========================================================================

    async def get_live_matches(self) -> list[MatchInfo]:
        """Fetch all currently live/recent matches from CricketData."""
        data = await self._request("currentMatches")
        matches: list[MatchInfo] = []

        for match_data in data.get("data", []):
            try:
                match_info = self._parse_match_info(match_data)
                matches.append(match_info)
            except (KeyError, ValueError) as e:
                logger.warning("Failed to parse match: %s — %s", match_data.get("id", "?"), e)
                continue

        logger.info("Fetched %d live matches from CricketData", len(matches))
        return matches

    async def get_match_scorecard(self, match_id: str) -> MatchState:
        """Fetch the full scorecard for a specific match."""
        data = await self._request("match_scorecard", {"id": match_id})
        match_data = data.get("data", {})
        return self._parse_match_state(match_data)

    async def get_ball_by_ball(self, match_id: str) -> list[BallEvent]:
        """Fetch ball-by-ball data for a match.

        Note: CricketData.org's ball-by-ball endpoint returns recent deliveries.
        The Delta Detector compares these against Redis state to find new balls.
        """
        data = await self._request("match_bbb", {"id": match_id})
        match_data = data.get("data", {})
        return self._parse_ball_by_ball(match_id, match_data)

    async def healthcheck(self) -> bool:
        """Verify the API is responding."""
        try:
            await self._request("currentMatches")
            return True
        except Exception:
            logger.exception("CricketData healthcheck failed")
            return False

    # =========================================================================
    # JSON → Domain Model Parsing
    # =========================================================================

    @staticmethod
    def _parse_match_info(data: dict) -> MatchInfo:
        """Parse raw API match data into our MatchInfo domain model."""
        teams = data.get("teams", [])
        team_info = data.get("teamInfo", [])

        home_team_data = team_info[0] if len(team_info) > 0 else {}
        away_team_data = team_info[1] if len(team_info) > 1 else {}

        return MatchInfo(
            match_id=data["id"],
            title=data.get("name", ""),
            format=_FORMAT_MAP.get(
                data.get("matchType", "").lower(),
                MatchFormat.T20,
            ),
            venue=data.get("venue", ""),
            team_home=TeamInfo(
                id=home_team_data.get("id", teams[0] if teams else ""),
                name=teams[0] if teams else "Unknown",
                short_name=home_team_data.get("shortname", ""),
                logo_url=home_team_data.get("img", ""),
            ),
            team_away=TeamInfo(
                id=away_team_data.get("id", teams[1] if len(teams) > 1 else ""),
                name=teams[1] if len(teams) > 1 else "Unknown",
                short_name=away_team_data.get("shortname", ""),
                logo_url=away_team_data.get("img", ""),
            ),
            start_time=_parse_datetime(data.get("dateTimeGMT")),
            series_name=data.get("series_id", ""),
        )

    def _parse_match_state(self, data: dict) -> MatchState:
        """Parse full scorecard data into MatchState."""
        match_info = self._parse_match_info(data)

        innings_list: list[InningsState] = []
        for idx, innings_data in enumerate(data.get("score", []), start=1):
            innings_list.append(self._parse_innings(idx, innings_data, match_info))

        status_str = data.get("status", "")
        match_started = data.get("matchStarted", False)

        return MatchState(
            match_info=match_info,
            status=_STATUS_MAP.get(
                status_str,
                MatchStatus.LIVE if match_started else MatchStatus.UPCOMING,
            ),
            current_innings=len(innings_list) if innings_list else 1,
            innings=innings_list,
            toss_winner=data.get("tossWinner", ""),
            toss_decision=data.get("tossChoice", ""),
            last_updated=datetime.utcnow(),
        )

    @staticmethod
    def _parse_innings(
        innings_number: int,
        data: dict,
        match_info: MatchInfo,
    ) -> InningsState:
        """Parse innings score data."""
        inning_name = data.get("inning", "")

        # Determine batting/bowling teams from innings name
        batting_team = match_info.team_home
        bowling_team = match_info.team_away
        if match_info.team_away.name.lower() in inning_name.lower():
            batting_team = match_info.team_away
            bowling_team = match_info.team_home

        runs = data.get("r", 0)
        wickets = data.get("w", 0)
        overs = data.get("o", 0.0)

        return InningsState(
            innings_number=innings_number,
            batting_team=batting_team,
            bowling_team=bowling_team,
            total_runs=runs,
            total_wickets=wickets,
            total_overs=float(overs),
            run_rate=round(runs / float(overs), 2) if float(overs) > 0 else 0.0,
        )

    @staticmethod
    def _parse_ball_by_ball(match_id: str, data: dict) -> list[BallEvent]:
        """Parse ball-by-ball API response into BallEvent list.

        CricketData.org returns ball-by-ball in a nested structure.
        We flatten and normalize it into our BallEvent model.
        """
        balls: list[BallEvent] = []

        bbb_data = data.get("bpidata", data.get("bbb", []))
        if isinstance(bbb_data, dict):
            bbb_data = [bbb_data]

        for ball_data in bbb_data:
            try:
                over_str = str(ball_data.get("overs", "0.0"))
                if "." in over_str:
                    over_parts = over_str.split(".")
                    over_num = int(over_parts[0])
                    ball_num = int(over_parts[1])
                else:
                    over_num = int(over_str)
                    ball_num = 0

                batsman_name = ball_data.get("batter", ball_data.get("batsman", "Unknown"))
                bowler_name = ball_data.get("bowler", "Unknown")

                runs = int(ball_data.get("runs", ball_data.get("score", 0)))
                is_six = runs == 6 or ball_data.get("six", False)
                is_four = runs == 4 or ball_data.get("four", False)

                # Parse wicket info if present
                wicket_info = None
                wicket_data = ball_data.get("wicket", None)
                if wicket_data and wicket_data != "":
                    wicket_type_str = str(wicket_data).lower() if isinstance(wicket_data, str) else ""
                    wicket_info = WicketInfo(
                        wicket_type=_WICKET_TYPE_MAP.get(wicket_type_str, WicketType.BOWLED),
                        batsman_out=PlayerInfo(
                            id="",
                            name=ball_data.get("wicketBatsman", batsman_name),
                        ),
                    )

                ball_event = BallEvent(
                    match_id=match_id,
                    innings=int(ball_data.get("innings", 1)),
                    over=over_num,
                    ball=ball_num,
                    batsman=PlayerInfo(id="", name=batsman_name),
                    bowler=PlayerInfo(id="", name=bowler_name),
                    runs_batsman=runs,
                    runs_total=runs,
                    is_boundary=is_four,
                    is_six=is_six,
                    is_wide=ball_data.get("wide", False) is True,
                    is_no_ball=ball_data.get("noball", False) is True,
                    wicket=wicket_info,
                    commentary_raw=ball_data.get("commentary", ""),
                    raw_data=ball_data,
                    timestamp=datetime.utcnow(),
                )
                balls.append(ball_event)

            except (KeyError, ValueError, TypeError) as e:
                logger.warning("Failed to parse ball data: %s — %s", ball_data, e)
                continue

        return balls

    async def close(self) -> None:
        """Close the HTTP client."""
        await self._client.aclose()


class CricketDataAPIError(Exception):
    """Raised when the CricketData.org API returns an error."""


def _parse_datetime(dt_str: str | None) -> datetime | None:
    """Parse ISO datetime string from the API."""
    if not dt_str:
        return None
    try:
        return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None

"""Database session factory and repository pattern.

Provides async SQLAlchemy session management and a Repository class
for CRUD operations on the persistent models.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from config.settings import get_settings
from cricket.db.models import Base, CommentaryLog, CostLog, MatchLog
from cricket.domain.models import AudioChunk, ClassifiedEvent, CommentaryLine

logger = logging.getLogger(__name__)


def _get_engine():
    """Create the async SQLAlchemy engine."""
    settings = get_settings()
    return create_async_engine(
        settings.db.dsn,
        echo=settings.debug,
        pool_size=5,
        max_overflow=10,
    )


_engine = None
_session_factory = None


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Get or create the session factory singleton."""
    global _engine, _session_factory
    if _session_factory is None:
        _engine = _get_engine()
        _session_factory = async_sessionmaker(
            _engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Get an async database session with automatic commit/rollback."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create all tables (run once at startup or in migrations)."""
    engine = _get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables created/verified.")


class CommentaryRepository:
    """Repository for persisting commentary and cost data.

    Usage:
        async with get_session() as session:
            repo = CommentaryRepository(session)
            await repo.log_commentary(classified_event, audio_chunk)
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def log_match(
        self,
        match_id: str,
        title: str,
        format: str,
        team_home: str,
        team_away: str,
        venue: str = "",
    ) -> MatchLog:
        """Create or get the MatchLog entry for a match."""
        from sqlalchemy import select

        stmt = select(MatchLog).where(MatchLog.match_id == match_id)
        result = await self._session.execute(stmt)
        match = result.scalar_one_or_none()

        if match is None:
            match = MatchLog(
                match_id=match_id,
                title=title,
                format=format,
                team_home=team_home,
                team_away=team_away,
                venue=venue,
            )
            self._session.add(match)
            await self._session.flush()

        return match

    async def log_commentary(
        self,
        event: ClassifiedEvent,
        chunk: AudioChunk,
    ) -> CommentaryLog:
        """Persist a commentary line with all context."""
        ball = event.ball_event

        log = CommentaryLog(
            match_id=ball.match_id,
            innings=ball.innings,
            over=ball.over,
            ball=ball.ball,
            over_display=ball.over_display,
            event_type=event.event_type.value,
            tier=event.tier.value,
            emotion=event.emotion.value,
            text=chunk.commentary.text,
            source=chunk.commentary.source,
            batsman=ball.batsman.display_name,
            bowler=ball.bowler.display_name,
            team_score=f"{event.team_score}/{event.team_wickets}",
            batsman_score=f"{event.batsman_runs}({event.batsman_balls})",
            llm_input_tokens=chunk.commentary.llm_input_tokens,
            llm_output_tokens=chunk.commentary.llm_output_tokens,
            llm_cost_usd=chunk.commentary.llm_cost_usd,
            tts_cost_usd=chunk.tts_cost_usd,
            audio_path=chunk.audio_path,
            audio_duration_ms=chunk.duration_ms,
        )
        self._session.add(log)
        await self._session.flush()
        return log

    async def log_cost(
        self,
        provider: str,
        operation: str,
        cost_usd: float,
        match_id: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        characters: int = 0,
    ) -> CostLog:
        """Persist an API call cost entry."""
        log = CostLog(
            match_id=match_id,
            provider=provider,
            operation=operation,
            cost_usd=cost_usd,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            characters=characters,
        )
        self._session.add(log)
        await self._session.flush()
        return log

    async def update_match_stats(
        self,
        match_id: str,
        balls_processed: int = 0,
        commentary_lines: int = 0,
        llm_calls: int = 0,
        template_calls: int = 0,
        total_cost_usd: float = 0.0,
    ) -> None:
        """Update the aggregated stats for a match."""
        from sqlalchemy import select, update

        stmt = (
            update(MatchLog)
            .where(MatchLog.match_id == match_id)
            .values(
                total_balls_processed=MatchLog.total_balls_processed + balls_processed,
                total_commentary_lines=MatchLog.total_commentary_lines + commentary_lines,
                total_llm_calls=MatchLog.total_llm_calls + llm_calls,
                total_template_calls=MatchLog.total_template_calls + template_calls,
                total_cost_usd=MatchLog.total_cost_usd + total_cost_usd,
            )
        )
        await self._session.execute(stmt)

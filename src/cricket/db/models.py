"""SQLAlchemy database models for persistent logging.

These are the PostgreSQL tables that store permanent records:
- Matches: metadata for every match the pipeline has covered
- CommentaryLogs: every commentary line generated (for replay/analysis)
- CostLogs: every API call cost (for billing dashboards)

The domain models (in domain/models.py) are the in-flight data.
These DB models are the at-rest persistence layer.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all DB models."""

    pass


class MatchLog(Base):
    """Persistent record of every match covered by the pipeline."""

    __tablename__ = "match_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(500))
    format: Mapped[str] = mapped_column(String(20))  # t20, odi, test
    venue: Mapped[str] = mapped_column(String(500), default="")
    team_home: Mapped[str] = mapped_column(String(100))
    team_away: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="upcoming")

    # Pipeline metrics
    total_balls_processed: Mapped[int] = mapped_column(Integer, default=0)
    total_commentary_lines: Mapped[int] = mapped_column(Integer, default=0)
    total_llm_calls: Mapped[int] = mapped_column(Integer, default=0)
    total_template_calls: Mapped[int] = mapped_column(Integer, default=0)
    total_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # Timestamps
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<MatchLog {self.match_id}: {self.title}>"


class CommentaryLog(Base):
    """Persistent record of every commentary line generated."""

    __tablename__ = "commentary_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String(100), index=True)

    # Ball identification
    innings: Mapped[int] = mapped_column(Integer)
    over: Mapped[int] = mapped_column(Integer)
    ball: Mapped[int] = mapped_column(Integer)
    over_display: Mapped[str] = mapped_column(String(10))

    # Event classification
    event_type: Mapped[str] = mapped_column(String(50))
    tier: Mapped[str] = mapped_column(String(20))  # major, minor, routine
    emotion: Mapped[str] = mapped_column(String(20))

    # Commentary
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20))  # "template" or "llm"

    # Players
    batsman: Mapped[str] = mapped_column(String(200), default="")
    bowler: Mapped[str] = mapped_column(String(200), default="")

    # Match context at time of commentary
    team_score: Mapped[str] = mapped_column(String(20), default="")
    batsman_score: Mapped[str] = mapped_column(String(20), default="")

    # Cost
    llm_input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    llm_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    tts_cost_usd: Mapped[float] = mapped_column(Float, default=0.0)

    # Audio
    audio_path: Mapped[str] = mapped_column(String(500), default="")
    audio_duration_ms: Mapped[int] = mapped_column(Integer, default=0)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<CommentaryLog {self.match_id} {self.over_display}: {self.text[:40]}>"


class CostLog(Base):
    """Persistent record of every API call cost."""

    __tablename__ = "cost_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[str] = mapped_column(String(100), index=True, default="")

    provider: Mapped[str] = mapped_column(String(50), index=True)  # bedrock, elevenlabs, etc.
    operation: Mapped[str] = mapped_column(String(50))  # commentary_gen, tts_render, data_poll
    cost_usd: Mapped[float] = mapped_column(Float)

    # Provider-specific metrics
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    characters: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    def __repr__(self) -> str:
        return f"<CostLog {self.provider}/{self.operation}: ${self.cost_usd:.6f}>"

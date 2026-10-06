"""SQLAlchemy async models for the interactive backtest platform.

Gracefully degrades to a no-op stub when DATABASE_URL is unset so the API
works without Postgres (results still served from Redis cache / in-process).

Tables:
  backtest_runs     persisted runs (param_hash unique key + metrics JSONB)
  comparison_sets   saved cross-strategy comparison views
  watchlists        saved symbol lists
  saved_views       arbitrary UI config blobs
"""
from __future__ import annotations

import os
import uuid
from contextlib import asynccontextmanager

_engine = None
_sessionmaker = None


def _db_url() -> str | None:
    return os.environ.get("DATABASE_URL")


async def _ensure_engine():
    global _engine, _sessionmaker
    if _engine is not None:
        return _engine
    url = _db_url()
    if not url:
        return None
    try:
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        pg = url.replace("postgresql://", "postgresql+asyncpg://").replace(
            "postgres://", "postgresql+asyncpg://"
        )
        _engine = create_async_engine(pg, pool_size=5, max_overflow=10, echo=False)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
        return _engine
    except Exception:
        return None


@asynccontextmanager
async def get_session():
    """Yields an async session, or None when the DB is unavailable."""
    if _sessionmaker is None:
        await _ensure_engine()
    if _sessionmaker is None:
        yield None
        return
    async with _sessionmaker() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ------------------------------------------------------------------ ORM models
try:
    from sqlalchemy import Column, DateTime, Index, String, Text, func
    from sqlalchemy.dialects.postgresql import JSONB, UUID
    from sqlalchemy.orm import DeclarativeBase

    class Base(DeclarativeBase):
        pass

    class BacktestRun(Base):
        __tablename__ = "backtest_runs"

        id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
        param_hash = Column(String(64), unique=True, nullable=False, index=True)
        strategy = Column(String(64), nullable=False)
        symbol = Column(String(32), nullable=False)
        timeframe = Column(String(16), nullable=False, default="1Day")
        amount = Column(String(32))
        start_date = Column(String(16))
        end_date = Column(String(16))
        params = Column(JSONB, default=dict)
        status = Column(String(16), default="done")
        metrics = Column(JSONB)
        error = Column(Text)
        created_at = Column(DateTime(timezone=True), server_default=func.now())

        __table_args__ = (
            Index("ix_bt_strategy_symbol", "strategy", "symbol"),
            Index("ix_bt_created_at", "created_at"),
        )

    class ComparisonSet(Base):
        __tablename__ = "comparison_sets"
        id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
        name = Column(String(128))
        run_ids = Column(JSONB, default=list)
        created_at = Column(DateTime(timezone=True), server_default=func.now())

    class Watchlist(Base):
        __tablename__ = "watchlists"
        id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
        name = Column(String(128))
        symbols = Column(JSONB, default=list)
        created_at = Column(DateTime(timezone=True), server_default=func.now())

    class SavedView(Base):
        __tablename__ = "saved_views"
        id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
        name = Column(String(128))
        config = Column(JSONB, default=dict)
        created_at = Column(DateTime(timezone=True), server_default=func.now())

    _HAS_MODELS = True

except ImportError:
    _HAS_MODELS = False
    Base = BacktestRun = ComparisonSet = Watchlist = SavedView = None  # type: ignore


async def create_tables() -> bool:
    if not _HAS_MODELS:
        return False
    eng = await _ensure_engine()
    if eng is None:
        return False
    try:
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return True
    except Exception:
        return False


async def persist_run(param_hash: str, body: dict, metrics: dict) -> str:
    """Upsert a backtest_run row. Returns the run_id (new or existing)."""
    if not _HAS_MODELS:
        return str(uuid.uuid4())
    async with get_session() as session:
        if session is None:
            return str(uuid.uuid4())
        try:
            from sqlalchemy import select
            q = await session.execute(
                select(BacktestRun).where(BacktestRun.param_hash == param_hash)
            )
            row = q.scalar_one_or_none()
            if row is None:
                row = BacktestRun(
                    param_hash=param_hash,
                    strategy=body.get("strategy", ""),
                    symbol=body.get("symbol", ""),
                    timeframe=body.get("timeframe", "1Day"),
                    amount=str(body.get("amount", "")),
                    start_date=body.get("start_date"),
                    end_date=body.get("end_date"),
                    params=body.get("params", {}),
                    status="done",
                    metrics=metrics,
                )
                session.add(row)
                await session.flush()
            else:
                row.metrics = metrics
                row.status = "done"
            return str(row.id)
        except Exception:
            return str(uuid.uuid4())


async def list_runs(limit: int = 50) -> list[dict]:
    if not _HAS_MODELS:
        return []
    async with get_session() as session:
        if session is None:
            return []
        try:
            from sqlalchemy import select
            q = await session.execute(
                select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(limit)
            )
            rows = q.scalars().all()
            return [
                {
                    "run_id": str(r.id),
                    "param_hash": r.param_hash,
                    "strategy": r.strategy,
                    "symbol": r.symbol,
                    "timeframe": r.timeframe,
                    "amount": r.amount,
                    "start_date": r.start_date,
                    "end_date": r.end_date,
                    "metrics": r.metrics,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ]
        except Exception:
            return []

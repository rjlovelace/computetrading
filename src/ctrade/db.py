"""Database models and session handling.

Trades and marks are append-only: an amendment writes a new trade version, and a corrected
mark writes a new row with a later `recorded_at`. Nothing is updated in place, so any past
state can be reconstructed.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

PRICE = Numeric(18, 6)
QTY = Numeric(18, 4)
MONEY = Numeric(20, 4)


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "products"

    code: Mapped[str] = mapped_column(String(40), primary_key=True)
    exchange: Mapped[str] = mapped_column(String(20))
    symbol: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200))
    gpu_model: Mapped[str] = mapped_column(String(40))
    index_name: Mapped[str] = mapped_column(String(200), default="")
    hours_rule: Mapped[str] = mapped_column(String(20))
    fixed_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tick_size: Mapped[Decimal] = mapped_column(PRICE)
    provisional: Mapped[bool] = mapped_column(Boolean, default=True)
    notes: Mapped[str] = mapped_column(Text, default="")


class Instrument(Base):
    __tablename__ = "instruments"

    instrument_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    product_code: Mapped[str] = mapped_column(ForeignKey("products.code"))
    kind: Mapped[str] = mapped_column(String(20))
    delivery_start: Mapped[date] = mapped_column(Date)
    delivery_end: Mapped[date] = mapped_column(Date)
    last_trade_date: Mapped[date] = mapped_column(Date)
    hours_per_contract: Mapped[Decimal] = mapped_column(QTY)


class Trade(Base):
    """One version of a trade. The live view is the highest version per trade_id."""

    __tablename__ = "trades"

    trade_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="live")  # live | cancelled
    book: Mapped[str] = mapped_column(String(60))
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"))
    side: Mapped[str] = mapped_column(String(4))  # buy | sell
    quantity: Mapped[Decimal] = mapped_column(QTY)  # unsigned contracts
    price: Mapped[Decimal] = mapped_column(PRICE)  # $ per GPU-hour
    fees: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    trade_date: Mapped[date] = mapped_column(Date)
    counterparty: Mapped[str] = mapped_column(String(100), default="")
    external_ref: Mapped[str] = mapped_column(String(100), default="")
    comment: Mapped[str] = mapped_column(Text, default="")
    entered_by: Mapped[str] = mapped_column(String(60), default="")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity if self.side == "buy" else -self.quantity


class Mark(Base):
    """An end-of-day mark ($/GPU-hour). Latest recorded_at wins for an instrument and date."""

    __tablename__ = "marks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instrument_id: Mapped[str] = mapped_column(ForeignKey("instruments.instrument_id"))
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    price: Mapped[Decimal] = mapped_column(PRICE)
    source: Mapped[str] = mapped_column(String(40), default="manual")
    comment: Mapped[str] = mapped_column(Text, default="")
    entered_by: Mapped[str] = mapped_column(String(60), default="")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DailyPnL(Base):
    """Stored result of a daily P&L run. Re-running a date replaces that date's rows."""

    __tablename__ = "daily_pnl"
    __table_args__ = (UniqueConstraint("as_of_date", "book", "instrument_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of_date: Mapped[date] = mapped_column(Date, index=True)
    prior_date: Mapped[date] = mapped_column(Date)
    book: Mapped[str] = mapped_column(String(60))
    instrument_id: Mapped[str] = mapped_column(String(100))
    quantity: Mapped[Decimal] = mapped_column(QTY)
    avg_price: Mapped[Decimal] = mapped_column(PRICE)
    mark: Mapped[Decimal | None] = mapped_column(PRICE, nullable=True)
    realized: Mapped[Decimal] = mapped_column(MONEY)
    unrealized: Mapped[Decimal] = mapped_column(MONEY)
    fees: Mapped[Decimal] = mapped_column(MONEY)
    cumulative: Mapped[Decimal] = mapped_column(MONEY)
    daily: Mapped[Decimal] = mapped_column(MONEY)
    new_trades: Mapped[Decimal] = mapped_column(MONEY)
    price_move: Mapped[Decimal] = mapped_column(MONEY)
    fees_today: Mapped[Decimal] = mapped_column(MONEY)
    amendments: Mapped[Decimal] = mapped_column(MONEY, default=Decimal("0"))
    residual: Mapped[Decimal] = mapped_column(MONEY)
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def database_url() -> str:
    return os.environ.get("DATABASE_URL", "sqlite:///ctrade.db")


_engines: dict[str, object] = {}


def get_engine(url: str | None = None):
    url = url or database_url()
    if url not in _engines:
        _engines[url] = create_engine(url)
    return _engines[url]


def init_db(url: str | None = None) -> None:
    Base.metadata.create_all(get_engine(url))


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    session = sessionmaker(bind=get_engine(url), expire_on_commit=False)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

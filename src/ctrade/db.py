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
from pathlib import Path

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
    make_url,
)
from sqlalchemy.exc import OperationalError
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


class MarketObservation(Base):
    """A price collected by an agent, e.g. one day's value of a GPU rental index.

    Append-only: a re-fetch writes a new row, and the latest `fetched_at` wins.
    """

    __tablename__ = "market_observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    series: Mapped[str] = mapped_column(String(100), index=True)  # e.g. ORNN.OCPI.H100 SXM
    obs_date: Mapped[date] = mapped_column(Date, index=True)
    value: Mapped[Decimal] = mapped_column(PRICE)
    unit: Mapped[str] = mapped_column(String(40), default="USD/GPU-hr")
    agent: Mapped[str] = mapped_column(String(40))
    method: Mapped[str] = mapped_column(String(40), default="api")  # api | web-extract | file
    source_url: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[str] = mapped_column(Text, default="")  # evidence, e.g. the quoted page text
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MarkProxy(Base):
    """Where to take a mark from when a contract has no price of its own.

    `target` is a product code (every contract month of it) or one instrument id. The source
    is another product's contract for the same delivery period, a specific instrument, or a
    price series such as SILICONDATA.H100. The mark is source x multiplier + spread.
    """

    __tablename__ = "mark_proxies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target: Mapped[str] = mapped_column(String(100), index=True)
    source_kind: Mapped[str] = mapped_column(String(20))  # product | instrument | series
    source: Mapped[str] = mapped_column(String(100))
    multiplier: Mapped[Decimal] = mapped_column(PRICE, default=Decimal("1"))
    spread: Mapped[Decimal] = mapped_column(PRICE, default=Decimal("0"))  # $ per GPU-hour
    priority: Mapped[int] = mapped_column(Integer, default=1)  # lower is tried first
    comment: Mapped[str] = mapped_column(Text, default="")
    entered_by: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Spread(Base):
    """A saved spread: a weighted sum of contract marks, e.g. GPU2 Jan27 - GPU1 Jan27.

    `legs` holds one "instrument_id weight" pair per line, e.g. "CME.GPU2.2027-01 1".
    """

    __tablename__ = "spreads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    legs: Mapped[str] = mapped_column(Text)
    entered_by: Mapped[str] = mapped_column(String(60), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AgentRun(Base):
    """One run of one agent, for monitoring."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    agent: Mapped[str] = mapped_column(String(40))
    as_of_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20))  # ok | skipped | failed
    observations: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


DEFAULT_URL = "sqlite:///ctrade.db"


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or DEFAULT_URL


def describe_url(url: str | None = None) -> str:
    """The database location without any password, e.g. for showing on screen."""
    parsed = make_url(url or database_url())
    if parsed.get_backend_name() == "sqlite":
        return f"SQLite file {Path(parsed.database or '').resolve()}"
    return f"{parsed.get_backend_name()} database {parsed.database} on {parsed.host}:{parsed.port}"


class DatabaseUnavailable(RuntimeError):
    """The database in DATABASE_URL can't be reached."""


def unavailable_message(url: str | None = None) -> str:
    return (
        f"Can't connect to the {describe_url(url)}. If you use Postgres, start it with "
        "`docker compose up -d`. To use a local file instead, put "
        f"`DATABASE_URL={DEFAULT_URL}` in your .env file."
    )


_engines: dict[str, object] = {}


def get_engine(url: str | None = None):
    url = url or database_url()
    if url not in _engines:
        # Fail fast instead of hanging when a database server isn't running.
        args = {"connect_timeout": 5} if url.startswith("postgres") else {}
        _engines[url] = create_engine(url, connect_args=args)
    return _engines[url]


def check_connection(url: str | None = None) -> None:
    """Raise DatabaseUnavailable with a plain explanation if the database can't be reached."""
    try:
        with get_engine(url).connect():
            pass
    except OperationalError as e:
        raise DatabaseUnavailable(unavailable_message(url)) from e


def init_db(url: str | None = None) -> None:
    """Create any missing tables. Existing tables and their data are left alone."""
    check_connection(url)
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

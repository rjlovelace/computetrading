"""Market data agents and the runner that stores what they collect."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import AgentRun, MarketObservation, utcnow
from .base import Agent, AgentSkipped, Observation
from .ornn import OrnnAgent
from .settlements import SettlementFileAgent
from .web_reader import WebReaderAgent

__all__ = ["Agent", "Observation", "all_agents", "run_agents", "latest_observations"]


def all_agents() -> list[Agent]:
    return [SettlementFileAgent(), OrnnAgent(), WebReaderAgent()]


def _store(session: Session, agent: Agent, observations: list[Observation]) -> int:
    """Store observations, skipping ones identical to the latest stored value."""
    stored = 0
    for obs in observations:
        latest = session.scalar(
            select(MarketObservation)
            .where(MarketObservation.series == obs.series,
                   MarketObservation.obs_date == obs.obs_date)
            .order_by(MarketObservation.fetched_at.desc(), MarketObservation.id.desc())
            .limit(1)
        )
        if latest is not None and latest.value == obs.value:
            continue
        session.add(
            MarketObservation(
                series=obs.series,
                obs_date=obs.obs_date,
                value=obs.value,
                unit=obs.unit,
                agent=agent.name,
                method=obs.method,
                source_url=obs.source_url,
                detail=obs.detail,
                fetched_at=utcnow(),
            )
        )
        stored += 1
    session.flush()
    return stored


def run_agents(
    session: Session,
    as_of: date,
    only: list[str] | None = None,
    backfill_days: int = 0,
    agents: list[Agent] | None = None,
) -> list[AgentRun]:
    """Run each agent; one agent failing never stops the others. Returns the run records."""
    runs = []
    for agent in agents if agents is not None else all_agents():
        if only and agent.name not in only:
            continue
        started: datetime = utcnow()
        run = AgentRun(agent=agent.name, as_of_date=as_of, started_at=started)
        try:
            observations = agent.collect(as_of, backfill_days)
            run.observations = _store(session, agent, observations)
            run.status = "ok"
            notes = [f"{len(observations)} fetched, {run.observations} new"]
            notes += getattr(agent, "rejections", [])
            run.message = "; ".join(notes)
        except AgentSkipped as e:
            run.status, run.message = "skipped", str(e)
        except Exception as e:  # report and carry on with the next agent
            run.status, run.message = "failed", f"{type(e).__name__}: {e}"
        run.finished_at = utcnow()
        session.add(run)
        runs.append(run)
    session.flush()
    return runs


def latest_observations(session: Session, on_or_before: date) -> list[MarketObservation]:
    """The newest value per series dated on or before the given date."""
    newest = (
        select(MarketObservation.series, func.max(MarketObservation.obs_date).label("d"))
        .where(MarketObservation.obs_date <= on_or_before)
        .group_by(MarketObservation.series)
        .subquery()
    )
    rows = session.scalars(
        select(MarketObservation)
        .join(newest, (MarketObservation.series == newest.c.series)
              & (MarketObservation.obs_date == newest.c.d))
        .order_by(MarketObservation.series, MarketObservation.fetched_at.desc())
    ).all()
    seen, out = set(), []
    for r in rows:
        if r.series not in seen:
            seen.add(r.series)
            out.append(r)
    return out

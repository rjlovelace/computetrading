# Compute Trading System

Trade entry, positions, marks and daily P&L for GPU compute contracts: the cash-settled
GPU rental index futures announced on CME, ICE and Nodal Exchange, plus OTC spot/term
rental blocks. The design is in [docs/design.md](docs/design.md).

## What's here (v0.1)

- **Reference data** for every compute contract announced on CME (GPU1, GPU2), ICE (HPR, BKL,
  COIL) and Nodal (Hopper, Blackwell). Specs not yet published by an exchange are flagged
  provisional in `src/ctrade/refdata.py`.
- **Trade blotter**: book, amend and cancel trades. Every change is a new version, so the
  full history is kept. Prices are checked against the contract's tick size.
- **Positions**: average-cost positions per book and instrument, with realized P&L on
  reducing trades and automatic close-out at final settlement after the last trade date.
- **Marks**: hand-entered or CSV-imported end-of-day marks ($/GPU-hour). A later entry for
  the same instrument and date replaces the earlier one without deleting it.
- **Daily P&L** with an explain: new trades, price move on yesterday's position, and fees.
  The run warns about missing marks and any unexplained residual.
- **Streamlit app** for all of the above, and a `ctrade` command line.

Market data capture and the automated curve builder come next (see design doc section 8).

## Quick start

```bash
# 1. Install (Python 3.12+)
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

# 2. Database: Postgres in Docker, or skip this for a local SQLite file
docker compose up -d
cp .env.example .env && export $(cat .env | xargs)

# 3. Create tables and load the contract catalogue
ctrade init

# 4. Use the app
streamlit run app/Home.py
```

## Command line

```bash
ctrade products
ctrade trade add --product ICE.HPR --month 2026-12 --side buy --qty 10 --price 2.150 --date 2026-11-02
ctrade instrument spot H100 2026-11-01 2026-11-30 --venue OTC
ctrade mark set ICE.HPR.2026-12 2026-11-02 2.180
ctrade mark import marks.csv        # instrument_id,as_of_date,price[,source]
ctrade trade import trades.csv      # instrument_id,side,quantity,price,trade_date[,book,fees,...]
ctrade positions --date 2026-11-02
ctrade pnl --date 2026-11-02
```

## Conventions

- Quantities are contracts. Futures contracts cover the product's hours per month (CME: 730
  fixed; ICE: the clock hours in the month). A spot contract is one GPU for every hour of its
  delivery period.
- Prices and marks are US dollars per GPU-hour. Value = price x hours per contract x contracts.
- Business days are Monday to Friday; exchange holiday calendars are not modelled yet.

## Tests

```bash
pytest
ruff check .
```

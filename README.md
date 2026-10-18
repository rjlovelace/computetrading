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

- **Market data agents** (v0.2) that collect GPU index prices from public sources and turn
  them into end-of-day marks. See below.

## Quick start

```bash
# 1. Install (Python 3.12+)
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

# 2. Database: Postgres in Docker, or skip this for a local SQLite file
docker compose up -d
cp .env.example .env    # Windows: copy .env.example .env

# 3. Create tables and load the contract catalogue
ctrade init

# 4. Use the app
streamlit run app/Home.py
```

`ctrade` and the app read settings such as `DATABASE_URL` and `ANTHROPIC_API_KEY` from the
`.env` file in the project folder, so nothing needs exporting. A variable already set in your
environment takes priority over `.env`. Keep `.env` private; it is in `.gitignore`.

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

## Market data agents

Agents collect prices; `ctrade mark build` turns them into marks. Every automatic mark records
its method and evidence, and a mark you enter by hand is never replaced.

| Agent | Source | Feeds | Needs |
|---|---|---|---|
| `ornn` | [Ornn OCPI](https://data.ornn.com) public API: daily H100 SXM, H200, B200, A100, RTX 5090 | ICE HPR / BKL futures, spot blocks | Nothing (free tier). `ORNN_API_KEY` for more history |
| `web` | Claude reads the public [Silicon Data](https://www.silicondata.com/products/silicon-index/h100) H100 and B200 index pages (list in `config/web_sources.toml`) | CME GPU1 / GPU2 futures | `ANTHROPIC_API_KEY` |
| `settlements` | Exchange settlement CSVs you drop in `data/settlements/` (`instrument_id,date,settle`) | Any futures contract | Files from your broker or the exchange |

How marks are built, in order: a hand-entered mark is kept; then an exchange settlement; then,
for futures, the expected monthly average of the settlement index (days already published are
fixed, the rest of the month assumed at the latest value); for spot blocks, the latest index
value. Index values older than 5 days aren't used, and a product is never marked off another
product's index. Nodal and ICE COIL contracts have no public source yet, so they stay manual.

The web agent keeps a value only if the passage Claude quotes really appears on the page,
contains the number, and the number is within the page's sanity range. Check each site's
terms of use before relying on scraped values.

```bash
ctrade agents list
ctrade agents run --backfill 90     # first run: load three months of Ornn history
ctrade mark build --date 2026-11-02
ctrade eod                          # agents, then marks, then the daily P&L, for today
```

The **Market data** page in the app does the same with buttons and shows the latest prices,
price history and recent agent runs.

### Running it every day

Run `ctrade eod` once a day after the indices publish (Ornn's daily print is stamped 20:00 UTC).

- **Mac/Linux** (`crontab -e`), weekdays at 5:30pm New York time on a machine set to that zone:
  `30 17 * * 1-5 cd /path/to/computetrading && .venv/bin/ctrade eod >> eod.log 2>&1`
- **Windows**: in Task Scheduler create a daily task that runs
  `C:\path\to\computetrading\.venv\Scripts\ctrade.exe eod` with "Start in" set to the
  project folder.

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

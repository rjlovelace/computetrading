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
- **v0.3**: a database that works with no setup, automatic marking whenever prices load,
  proxy marks (mark one contract off another), a **Mark history** chart for every contract,
  and $1,234.56 number formats on every screen and in the command line.
- **v0.4**: a **Charts** page with term structures (and their day over day change), saved
  spreads with their daily change, and simple or exponential moving averages. See below.
- **v0.5**: forward power for PJM, MISO and ERCOT (more ISOs by configuration), loaded from
  CSV, charted and spread like compute, and the **energy cost** of each compute contract.

## Quick start

```bash
# 1. Install (Python 3.12+)
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

# 2. Settings. The example stores data in a local file, ctrade.db, with no other setup.
cp .env.example .env    # Windows: copy .env.example .env

# 3. Create tables and load the contract catalogue
ctrade init

# 4. Use the app
streamlit run app/Home.py
```

`ctrade` and the app read settings such as `DATABASE_URL` and `ANTHROPIC_API_KEY` from the
`.env` file in the project folder, so nothing needs exporting. A variable already set in your
environment takes priority over `.env`. Keep `.env` private; it is in `.gitignore`.

### Database

Everything (trades, marks, prices, P&L runs, proxy rules) is stored in the database named by
`DATABASE_URL`. The default is the SQLite file `ctrade.db` in the project folder; back it up by
copying that file. For Postgres, run `docker compose up -d` and switch `DATABASE_URL` in `.env`
to the Postgres line. The Home page shows which database is in use and how much it holds.
New versions add their tables automatically the next time you run `ctrade` or open the app;
existing data is kept. If the database can't be reached you get a plain message saying so.

## Command line

```bash
ctrade products
ctrade trade add --product ICE.HPR --month 2026-12 --side buy --qty 10 --price 2.150 --date 2026-11-02
ctrade instrument spot H100 2026-11-01 2026-11-30 --venue OTC
ctrade instrument months ICE.HPR CME.GPU1 NODAL.H100 --first 2026-11 --count 12
ctrade mark set ICE.HPR.2026-12 2026-11-02 2.180
ctrade mark import marks.csv        # instrument_id,as_of_date,price[,source]
ctrade trade import trades.csv      # instrument_id,side,quantity,price,trade_date[,book,fees,...]
ctrade positions --date 2026-11-02
ctrade pnl --date 2026-11-02
```

## Market data agents

Agents collect prices, and every contract is marked as soon as they load: `ctrade agents run`
(and the **Load prices and mark** button) rebuilds the marks for each day that got new prices,
through the run date. Entering or importing a mark by hand also rebuilds that day, so contracts
proxied off it follow. Every automatic mark records its method and evidence, and a mark you
enter by hand is never replaced.

| Agent | Source | Feeds | Needs |
|---|---|---|---|
| `ornn` | [Ornn OCPI](https://data.ornn.com) public API: daily H100 SXM, H200, B200, A100, RTX 5090 | ICE HPR / BKL futures, spot blocks | Nothing (free tier). `ORNN_API_KEY` for more history |
| `web` | Claude reads the public [Silicon Data](https://www.silicondata.com/products/silicon-index/h100) H100 and B200 index pages (list in `config/web_sources.toml`) | CME GPU1 / GPU2 futures | `ANTHROPIC_API_KEY` |
| `settlements` | Exchange settlement CSVs you drop in `data/settlements/` (`instrument_id,date,settle`) | Any futures contract | Files from your broker or the exchange |

How marks are built, in order: a hand-entered mark is kept; then an exchange settlement; then,
for futures, the expected monthly average of the settlement index (days already published are
fixed, the rest of the month assumed at the latest value); for spot blocks, the latest index
value; then any proxy rules you set up (below). Index values older than 5 days aren't used.
Every contract in the database is marked, traded or not, so you can chart contracts before
trading them. Nodal and ICE COIL contracts have no public source of their own.

### Proxy marks

A proxy rule marks a contract from somewhere else when it has no price of its own: another
product's contract for the same month, one specific instrument, or a price series. The mark is
source x multiplier + spread, and rules are tried in priority order.

```bash
ctrade proxy add NODAL.H100 CME.GPU1                 # Nodal H100 follows CME GPU1 (Silicon Data H100)
ctrade proxy add NODAL.B200 series:ORNN.OCPI.B200 --spread -0.05 --priority 2
ctrade proxy list
ctrade proxy remove 2
```

The **Marks** page has the same controls.

## Charts

The **Charts** page has three tabs:

- **Mark history**: each contract's mark over time.
- **Term structure**: pick a product, such as CME.GPU1, to see one line across its contract
  months. Add curve dates to compare curves, and the second chart shows each date's
  change from the previous marked date, month by month.
- **Spreads**: create a spread from two or three contracts with weights, such as
  CME.GPU2 Jan27 - CME.GPU1 Jan27, then chart its value and its daily change. Spreads are
  saved, so they are there next time.

Mark history and spreads can show moving averages: SMA (simple average of the last N marks)
or EMA (exponential, weighting recent marks more). Pick from the list or type others, e.g.
SMA 50. Each average is drawn as a dashed line.

```bash
ctrade curve CME.GPU1 --dates 3                       # last 3 curves and today's change
ctrade spread add --leg "CME.GPU2 Jan27:1" --leg "CME.GPU1 Jan27:-1"
ctrade spread list
ctrade spread show 1 --days 20 --ma "SMA 5" --ma "EMA 10"
ctrade spread remove 1
```

The web agent keeps a value only if the passage Claude quotes really appears on the page,
contains the number, and the number is within the page's sanity range. Check each site's
terms of use before relying on scraped values.

```bash
ctrade agents list
ctrade agents run --backfill 90     # first run: three months of history, marked day by day
ctrade mark build --date 2026-11-02 # rebuild one day's marks without fetching
ctrade eod                          # prices and marks, then the daily P&L, for today
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

## Forward power

Forward power curves sit alongside compute: each ISO's hubs and zones, on-peak, off-peak and
around-the-clock (ATC), day-ahead or real-time, physical or financial. A power product is one
of each, e.g. `PJM.WESTERNHUB.DA.F.ONPK`; its contract months are normal instruments, so
marks, positions, P&L, charts and spreads all work the same way. Prices are $/MWh, quantities
are MW, and a contract covers the month's hours in its time of use.

There's no free public source for forward power curves (exchange settlements need a licence),
so they load from CSV, on the **Power** page or with `ctrade power load`. Files dropped in
`data/power/` are also loaded by `ctrade eod`. Columns (`ctrade power template` prints an
example):

| column | example | notes |
|---|---|---|
| trade_date | 2026-10-23 | the curve date |
| iso | PJM | PJM, MISO, ERCOT, or any ISO in `config/isos.toml` |
| contract_month | 2027-01 | or Jan27, Jan-27, 01/2027 |
| settlement_point | Western Hub | hub, zone or node name |
| point_type | hub | optional |
| settlement | Day-Ahead | or Real-Time (DA, RT) |
| delivery | Financial | or Physical |
| time_of_use | onpeak | offpeak or ATC (5x16, 7x8, 7x24 also work) |
| price | 78.50 | $/MWh |

Every row is checked before anything loads, and a file with errors lists them all. Each price
is stored as that contract's settlement for the date, so it becomes the mark (a mark entered
by hand still wins) and the day's marks are rebuilt. Reloading a file changes nothing.

Hours per contract come from `config/isos.toml`: PJM and MISO peak is HE8-HE23 and ERCOT
HE7-HE22, Monday to Friday excluding NERC holidays. PJM and ERCOT follow daylight saving
(March has one hour fewer, November one more); MISO runs on Eastern Standard Time all year.
Add another ISO by adding a block to that file. Power contracts stop trading on the last
business day before delivery, so a position closes at that day's mark.

### Energy cost of compute

    energy $/GPU-hour = power $/MWh x kW per GPU x PUE / 1,000

The **Energy cost** tab on the Charts page compares a compute curve, such as CME.GPU1, with a
power curve for the same months. It shows the energy cost, the all-in cost (compute mark plus
energy) per GPU-hour, and both per contract (730 GPU-hours for CME). None of the compute
contract specs publish an energy factor, so the defaults are NVIDIA DGX maximum system power
per GPU (H100 and H200 1.275 kW, B200 1.8 kW, A100 0.8125 kW) with a PUE of 1.3. Change them
on the Power page or with `ctrade energy set`.

```bash
ctrade power template curves.csv     # example file to fill in
ctrade power load curves.csv
ctrade power list
ctrade power hours PJM 2027-03       # on-peak, off-peak and ATC hours
ctrade curve PJM.WESTERNHUB.DA.F.ONPK
ctrade spread add --leg "PJM.WESTERNHUB.DA.F.ONPK Jul27:1" --leg "PJM.WESTERNHUB.DA.F.OFFPK Jul27:-1"
ctrade energy factors
ctrade energy set H100 1.3 --pue 1.25
ctrade energy cost CME.GPU1 PJM.WESTERNHUB.DA.F.ATC
```

## Conventions

- Quantities are contracts. Futures contracts cover the product's hours per month (CME: 730
  fixed; ICE: the clock hours in the month). Power quantities are MW. A spot contract is one GPU for every hour of its
  delivery period.
- Prices and marks are US dollars per GPU-hour, shown to four decimals ($2.5900). Money is
  shown to the cent ($1,234.56). Value = price x hours per contract x contracts.
- Business days are Monday to Friday; exchange holiday calendars are not modelled yet.

## Tests

```bash
pytest
ruff check .
```

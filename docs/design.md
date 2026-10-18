# Compute Trading System: Architecture and Data Model (v0.1 draft)

Status: draft v0.2, 2026-10-17. Updated with Russell's answers: exchange futures plus spot, starting with compute contracts on CME, ICE and Nodal; Python + Postgres + Streamlit confirmed. Nothing has been built yet; no repo or external services are connected.

## 1. Goal and scope

A system to trade GPU/compute capacity that covers five jobs:

1. **Capture and analyze market data**: prices, quotes and indices for GPU-hours by chip, region and tenor.
2. **Make trades**: enter and amend trades in a blotter with full audit history.
3. **Calculate positions**: net exposure by instrument and by delivery period (GPU-hours per day/month).
4. **Mark contracts**: build a forward curve per GPU type and value every open contract against it.
5. **Run a daily P&L**: realized plus unrealized, with an explain of why it moved.

Out of scope for v1 (can come later): order routing to exchanges, options pricing, credit/margining, multi-entity accounting.

## 2. How compute contracts differ from typical commodities

Compute behaves most like **power or natural gas**: a non-storable commodity delivered over a period. That shapes the design.

- **Delivery is a strip of hours**, not a point in time. A "Dec-26 H100 block" is ~744 hours x N GPUs. Positions must be bucketed by delivery period.
- **Quality matters**: H100 SXM vs PCIe, B200, GB200 NVL72, interconnect (InfiniBand vs Ethernet), region, provider tier. Each combination is a separate product, linked to a reference product by a spread.
- **Hardware depreciates**: each generation's price trends down as newer chips arrive, so forward curves are typically backwardated and long-dated marks need a model.
- **Liquidity is thin and fragmented**: spot rental listings, bilateral term deals, and a small number of indices and exchange-style venues. Marks will often be Level 2/3 (model-based), so methodology and audit trail matter.
- **Settlement varies**: physical (you get/give cluster access), cash-settled against an index, or a hybrid.

## 3. Architecture

```
  Market data sources                    Users
  (indices, venues, broker quotes,        |
   spot rental listings, own trades)      v
        |                           +-------------+
        v                           | Trade entry |  (UI / CLI / API)
  +-------------+                   +------+------+
  | Ingestion   |  normalize, dedupe,      |
  | adapters    |  validate                v
  +------+------+                   +-------------+
         v                          | Trade store |  append-only, versioned
  +-------------+                   +------+------+
  | Market data |                          |
  | store       |  raw + normalized        v
  +------+------+                   +-------------+
         v                          | Position    |  by product x delivery bucket
  +-------------+                   | engine      |
  | Curve       |  marks per product+------+------+
  | builder     |  and tenor               |
  +------+------+                          v
         +--------------------------> +-------------+
                                      | Valuation / |  MTM per trade
                                      | marking     |
                                      +------+------+
                                             v
                                      +-------------+
                                      | Daily P&L + |  realized, unrealized,
                                      | explain     |  attribution, reports
                                      +-------------+
```

**Components**

| Component | Responsibility |
|---|---|
| Ingestion adapters | One adapter per source. Pull or receive data, store the raw payload, map to normalized ticks. Run on a schedule. |
| Market data store | Raw payloads (for audit/replay) plus normalized observations: product, tenor, price, size, type (trade/bid/ask/index), source, timestamp. |
| Analytics | Notebooks/dashboards over the store: price history, spreads between chips/regions, curve shape, volatility, liquidity. |
| Trade entry | Book, amend, cancel trades. Every change is a new version; nothing is overwritten. |
| Position engine | Explodes each trade into delivery buckets (GPU-hours per day) and nets by product, book and bucket. |
| Curve builder | Produces an end-of-day (EOD) forward curve per product from observations, using the marking hierarchy in section 5. Snapshots are immutable and versioned. |
| Valuation | Values each open trade against the curve snapshot: (curve price - trade price) x remaining GPU-hours x direction, discounted if desired. |
| P&L and reporting | Daily P&L by trade, book and product; explain; exports. |

**Key design principles**

- **Bitemporal and append-only.** Trades, marks and curves keep both "effective as of" and "recorded at" times. You can rerun any past day's P&L exactly as it looked then, or with corrections.
- **EOD snapshot as the unit of truth.** Each business day closes with a frozen set of curves and a frozen position, and P&L is the difference between two snapshots.
- **Products as data, not code.** New GPU types, regions or tenors are rows in a table, not new code paths.

## 4. Core data model

```
Product (the tradable commodity)
  product_id, gpu_model (H100-SXM, B200, ...), interconnect, region,
  provider_tier, unit (GPU-hour), reference_product_id (for spread marking)

Instrument / Contract (a standard or bespoke contract on a product)
  instrument_id, product_id, contract_type (spot | forward | swap | index_future),
  delivery_start, delivery_end, delivery_shape (24x7, custom calendar),
  settlement (physical | cash | index), index_id (if cash-settled),
  venue (bilateral | exchange name), currency

Counterparty
  counterparty_id, name, type (provider, buyer, broker, exchange)

Book
  book_id, name, strategy, owner

Trade (versioned)
  trade_id, version, status (live | amended | cancelled),
  instrument_id, book_id, counterparty_id,
  side (buy | sell), quantity_gpus, price_per_gpu_hour, fees,
  trade_time, entered_by, recorded_at, comment

DeliveryBucket (derived from trades)
  trade_id, version, bucket_date, gpu_hours

MarketObservation
  obs_id, source, product_id, tenor (spot | date range), obs_type
  (trade | bid | ask | mid | index | broker_quote), price, size,
  observed_at, ingested_at, raw_ref

CurveSnapshot
  curve_id, product_id, as_of_date, version, method, created_at, approved_by

CurvePoint
  curve_id, tenor_start, tenor_end, price, source_level (L1 | L2 | L3), inputs_ref

Position (derived, per EOD)
  as_of_date, book_id, product_id, bucket (day or month), net_gpu_hours, avg_price

Valuation (per trade per EOD)
  as_of_date, trade_id, version, curve_id, remaining_gpu_hours, mtm_value

PnL (per trade per EOD)
  as_of_date, trade_id, realized, unrealized, total,
  explain: new_trade, curve_move, time_decay_delivery, amendments, fees
```

## 5. Marking methodology

Options, in order of preference, applied as a **hierarchy per curve point**:

1. **Observed trades (Level 1-ish).** Volume-weighted price of trades in that product/tenor on the day, if there are enough and they are recent.
2. **Executable quotes.** Mid of best bid/ask from venues, or broker quotes. Flag a wide bid/ask spread.
3. **Index.** Published GPU rental indices as the spot anchor (and the settlement price for cash-settled contracts).
4. **Spread to a reference curve (Level 2).** Mark thin products as reference curve + spread, e.g. H100-PCIe = H100-SXM - $x, or Region B = Region A + basis. Spreads come from recent observations.
5. **Model (Level 3).** For long tenors with no data: extrapolate from the liquid front using a decay or depreciation curve (e.g. price declines at a fitted monthly rate as the next generation ramps up), or interpolate between known points.

Supporting rules:

- **Interpolation** between tenors: linear in price to start; can move to a smooth fit later.
- **Staleness**: an observation older than N days drops to the next level.
- **Overrides**: a trader or risk manager can override a point with a reason, and it is logged and shown in reports.
- **Independent price verification**: a monthly comparison of marks against realized trades, to keep the model honest.
- **Valuation of a trade** = sum over remaining delivery buckets of (curve price for that bucket - trade price) x GPU-hours x (+1 buy / -1 sell). Discounting is optional at first since tenors are short.

## 6. Daily P&L flow

Run for each business day T after the market close cut-off:

1. **Freeze trades**: snapshot all trade versions recorded up to the cut-off.
2. **Ingest and validate market data** for T; flag gaps and outliers.
3. **Build curves** for T via the hierarchy above; review and approve (overrides logged).
4. **Compute positions**: explode trades into buckets; split each into delivered-to-date and remaining.
5. **Value**: MTM each live trade against T's curves.
6. **P&L** = (MTM_T + realized_T) - (MTM_T-1 + realized_T-1), per trade.
   - **Realized**: delivered hours settled at (settlement/index price - trade price), or for physical delivery the hours consumed or resold.
   - **Unrealized**: change in MTM of remaining hours.
7. **Explain** (attribution) so that the components sum to the total:
   - New trades: MTM at T of trades booked on T (day-one P&L vs curve).
   - Curve move: yesterday's remaining position x change in curve.
   - Delivery/roll: hours that moved from unrealized to realized.
   - Amendments and cancels.
   - Fees and costs.
   - Unexplained residual (should be ~0; a non-zero residual is a bug signal).
8. **Report and sign off**: P&L by book/product/trade, position ladder, curve chart vs prior day, overrides list. Lock day T.

## 7. Suggested tech stack (default, open to change)

- **Language**: Python, which suits data ingestion, analytics and curve fitting.
- **Storage**: PostgreSQL, keeping the append-only/bitemporal tables above. DuckDB plus Parquet is a lighter option for a solo start.
- **Scheduling**: cron or a simple scheduler at first; Prefect/Airflow if sources multiply.
- **UI**: Streamlit for blotter, curves and P&L dashboards; a proper web app later if others use it.
- **Testing**: golden-file tests for P&L (known trades + known curves = known P&L), since correctness here is non-negotiable.

## 8. Suggested build order

1. Product/instrument reference data and trade blotter (manual entry).
2. Position engine with delivery buckets.
3. Manual curve entry plus valuation, which gives a working daily P&L on day one with hand-entered marks.
4. Market data ingestion for the first source, plus analytics notebooks.
5. Automated curve builder with the marking hierarchy.
6. P&L explain, reports and sign-off workflow.

Steps 1-3 give a usable book-of-record quickly; data automation follows.

## 9. Target contracts (researched 2026-10-17)

All three exchanges have announced cash-settled GPU rental index futures. **As of today none is confirmed trading**: each is waiting on regulatory review, and the CFTC has said it will run a public comment process on compute derivatives. Verify status on the exchange pages before go-live.

| Exchange | Contract | Code | Underlying index | Size / quote | Months | Status |
|---|---|---|---|---|---|---|
| CME (NYMEX) | Silicon Data H100 Rental Index Futures | GPU1 (index SDH100RT) | Silicon Data Daily H100 Rental Index (neocloud on-demand rates) | 730 GPU-hours; $7.30 per tick (implies $0.01/GPU-hr) | Monthly, 36 out | Targeted Oct 5, 2026; CME page still says pending review |
| CME (NYMEX) | Silicon Data B200 Rental Index Futures | GPU2 (index SDRB200RT) | Silicon Data Daily B200 Rental Index | Not yet published | Not yet published | Same as above |
| ICE Futures U.S. | OCPI H100 Future | HPR | Ornn Compute Price Index H100 | Hours in contract month; $0.001/GPU-hr tick | Up to 24 monthly | No date; ICE awaiting CFTC comment period. OCPI data and hypothetical settlements on ICE platform from Oct 5, 2026 |
| ICE Futures U.S. | OCPI B200 Future | BKL | Ornn Compute Price Index B200 | Hours in contract period; $0.001/GPU-hr tick | Up to 24 monthly | Same as above |
| ICE Futures U.S. | GPU compute futures on NATIVX COIL index | TBD | NATIVX COIL (energy-normalised GPU price) | Not published | Not published | Announced Jul 1, 2026; later in 2026 pending approval |
| Nodal Exchange | GPU compute futures (Hopper, Blackwell) | TBD | Compute Desk daily blended GPU rental indexes | Not published | Not published | Announced Sep 4, 2026; "this year", pending approval |

**What this means for the design**

- Every listed contract is **cash-settled against a monthly average of a daily index**. So a contract is a monthly averaging swap, the same shape as many power and gas futures. Final settlement = average of daily index prints in the month; P&L per contract = (final settlement - trade price) x hours in month x contracts.
- **Marking becomes mostly Level 1**: use exchange daily settlement prices for each listed month. The curve builder only needs to interpolate/extrapolate for spot and any OTC tenors.
- **Partially-fixed months matter**: inside the contract month, part of the average is already known from published index prints. The position engine should track fixed vs unfixed days so intra-month risk is correct.
- **Indices are the market data backbone**: Silicon Data (H100, B200), Ornn OCPI (H100, H200, B200, RTX 5090), NATIVX COIL, and Compute Desk. Daily index prints are needed for both settlement and spot marking.
- **Basis between venues** is a real risk: an H100 on CME (Silicon Data) and an H100 on ICE (Ornn) settle on different indices. Model them as separate products linked by a basis spread.
- **Spot contracts** are not listed on these three exchanges. Spot will be OTC or marketplace rentals, priced off the same daily indices.
- **Contract size varies**: CME uses a fixed 730 hours; ICE uses the actual hours in the month (720/744/672). The instrument table needs a `hours_rule` field (fixed vs calendar).
- **Exchange data access** (settlements, trades) needs market data licences or a broker/FCM feed; index data needs licences from each index provider.

## 10. Repository and project setup

GitHub is not linked to this project yet, so I can't see or create repos. Suggested setup:

1. **Create one private repo** under your personal GitHub account, e.g. `compute-trading` (a monorepo keeps ingestion, engine and UI in step while it is just you).
2. **Connect GitHub to Claude** at https://claude.ai/connect-github and install the Claude GitHub App on that repo, then tell me the `owner/repo` name. I'll add it to this project and open PRs there for your review; nothing merges without you.
3. **Proposed layout**

```
compute-trading/
  pyproject.toml            # uv or poetry; Python 3.12
  docker-compose.yml        # local Postgres 16
  .env.example              # DB URL, data source keys (never commit real keys)
  db/migrations/            # Alembic migrations (schema from section 4)
  src/ctrade/
    refdata/                # products, instruments, contract specs, calendars
    marketdata/adapters/    # one module per source: cme, ice, nodal, silicon_data, ornn, ...
    trades/                 # blotter: book, amend, cancel (versioned)
    positions/              # delivery buckets, fixed vs unfixed days
    marking/                # curve builder, marking hierarchy, overrides
    pnl/                    # daily P&L, explain, EOD close
    jobs/                   # scheduled EOD run
  app/                      # Streamlit pages: blotter, positions, curves, P&L
  tests/                    # unit tests + golden-file P&L tests
  docs/                     # this design doc, runbooks
  .github/workflows/ci.yml  # lint (ruff), type-check, tests on every PR
```

4. **Working rules**: `main` protected, all changes via PR, CI must pass; secrets in a `.env` locally and GitHub Actions secrets in CI.
5. **Hosting later**: start fully local (Docker Postgres + Streamlit on your laptop). When you want it always-on, a small managed Postgres plus a container host is enough; decide once the EOD job is working.

## 11. Decisions still needed from Russell

1. **Repo**: create the private repo (name suggestion: `compute-trading`), connect GitHub, and send me `owner/repo`.
2. **Data access**: which exchange data you'll get (direct licence, broker/FCM statements, or manual entry to start) and which index providers you have or will license.
3. **Spot trading**: which venues or counterparties you'll trade spot with, and whether spot is physical rentals or financial.
4. **Clearing broker / FCM**: who clears your futures. Their daily statements are the reconciliation source for trades and settlements.
5. **P&L conventions**: currency (assume USD), EOD cut-off and timezone (assume exchange settlement time, US Central), business-day calendar.
6. **Users**: just you for now (assumed), or others who need logins and sign-off.

## Sources

- CME product page: https://www.cmegroup.com/markets/energy/power/compute-futures.html
- CME press release (Aug 11, 2026): https://www.cmegroup.com/media-room/press-releases/2026/8/11/cme_group_and_silicondatatolaunchcomputefuturesonoctober5tounloc.html
- ICE exchange notice, OCPI compute futures (Sep 29, 2026): https://www.ice.com/publicdocs/futures_us/exchange_notices/ICE_Futures_US_Exchange_Notice_ORNN_Compute_4Q2026_20260929.pdf
- ICE and Ornn announcement (May 19, 2026): https://www.stocktitan.net/news/ICE/ice-and-ornn-to-launch-gpu-compute-futures-ketjg9g0zpyz.html
- ICE and NATIVX announcement (Jul 1, 2026): https://www.leaprate.com/financial-services/exchanges/ice-and-nativx-to-launch-gpu-compute-futures-contracts-tied-to-energy-normalised-index/
- Nodal and Compute Desk announcement (Sep 4, 2026): https://www.marketsmedia.com/nodal-exchange-compute-desk-to-launch-compute-futures/
- CFTC review of compute derivatives: https://www.financemagnates.com/institutional-forex/cftc-plans-ai-compute-derivatives-consultation-as-cme-targets-october-5/

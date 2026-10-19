"""Command-line interface: `ctrade --help`."""

from __future__ import annotations

import argparse
import csv
import getpass
import sys
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from . import agents, marking, services
from .db import DatabaseUnavailable, Product, describe_url, init_db, session_scope
from .formatting import adjustment, money, number, price


def _d(text: str) -> date:
    return date.fromisoformat(text)


def _resolve_instrument(session, args) -> str:
    if args.instrument:
        return args.instrument
    if args.product and args.month:
        return services.ensure_future(session, args.product, args.month).instrument_id
    raise SystemExit("Give --instrument, or --product and --month")


def cmd_init(args) -> None:
    init_db()
    with session_scope() as s:
        n = services.seed_products(s)
    print(f"Database ready ({describe_url()}); {n} products loaded.")


def cmd_products(args) -> None:
    with session_scope() as s:
        for p in s.scalars(select(Product).order_by(Product.code)):
            flag = " (provisional)" if p.provisional else ""
            print(f"{p.code:12} {p.name}{flag}")


def cmd_instrument(args) -> None:
    with session_scope() as s:
        if args.kind == "future":
            inst = services.ensure_future(s, args.product, args.month)
        else:
            inst = services.ensure_spot(s, args.gpu, _d(args.start), _d(args.end), args.venue)
        print(f"{inst.instrument_id}  hours/contract={number(inst.hours_per_contract)}  "
              f"last trade {inst.last_trade_date}")


def cmd_instrument_months(args) -> None:
    with session_scope() as s:
        for code in args.products:
            for inst in services.ensure_months(s, code, args.first, args.count):
                print(inst.instrument_id)


def cmd_trade_add(args) -> None:
    with session_scope() as s:
        t = services.book_trade(
            s,
            instrument_id=_resolve_instrument(s, args),
            side=args.side,
            quantity=Decimal(args.qty),
            price=Decimal(args.price),
            trade_date=_d(args.date),
            book=args.book,
            fees=Decimal(args.fees),
            counterparty=args.cpty,
            external_ref=args.ref,
            comment=args.comment,
            entered_by=getpass.getuser(),
        )
        print(f"Booked trade {t.trade_id}: {t.side} {number(t.quantity)} {t.instrument_id} "
              f"@ {price(t.price)}")


def cmd_trade_amend(args) -> None:
    changes = {}
    for field, conv in (("qty", Decimal), ("price", Decimal), ("fees", Decimal), ("date", _d),
                        ("side", str), ("book", str), ("comment", str)):
        value = getattr(args, field)
        if value is not None:
            key = {"qty": "quantity", "date": "trade_date"}.get(field, field)
            changes[key] = conv(value)
    with session_scope() as s:
        t = services.amend_trade(s, args.trade_id, entered_by=getpass.getuser(), **changes)
        print(f"Trade {t.trade_id} is now version {t.version}")


def cmd_trade_cancel(args) -> None:
    with session_scope() as s:
        t = services.cancel_trade(s, args.trade_id, getpass.getuser(), args.comment)
        print(f"Trade {t.trade_id} cancelled (version {t.version})")


def cmd_trades(args) -> None:
    with session_scope() as s:
        for t in services.live_trades(s):
            print(f"{t.trade_id:>5} v{t.version} {t.trade_date} {t.book:8} {t.side:4} "
                  f"{number(t.quantity):>8} {t.instrument_id:28} @ {price(t.price)}")


def cmd_trade_import(args) -> None:
    """CSV columns: instrument_id,side,quantity,price,trade_date
    plus optional book,fees,counterparty,external_ref."""
    n = 0
    with session_scope() as s, open(args.path, newline="") as f:
        for row in csv.DictReader(f):
            services.book_trade(
                s,
                instrument_id=row["instrument_id"],
                side=row["side"].strip().lower(),
                quantity=Decimal(row["quantity"]),
                price=Decimal(row["price"]),
                trade_date=_d(row["trade_date"]),
                book=row.get("book") or "MAIN",
                fees=Decimal(row.get("fees") or "0"),
                counterparty=row.get("counterparty") or "",
                external_ref=row.get("external_ref") or "",
                entered_by=getpass.getuser(),
            )
            n += 1
    print(f"Imported {n} trades")


def _remark(session, dates) -> None:
    """Rebuild automatic marks so contracts proxied off a hand-entered mark follow it."""
    for d in sorted(set(dates)):
        marking.build_marks(session, d)


def cmd_mark_set(args) -> None:
    with session_scope() as s:
        services.set_mark(s, args.instrument, _d(args.date), Decimal(args.price),
                          source=args.source, comment=args.comment,
                          entered_by=getpass.getuser())
        _remark(s, [_d(args.date)])
    print("Mark saved")


def cmd_mark_import(args) -> None:
    """CSV columns: instrument_id,as_of_date,price[,source]"""
    dates = []
    with session_scope() as s, open(args.path, newline="") as f:
        for row in csv.DictReader(f):
            services.set_mark(s, row["instrument_id"], _d(row["as_of_date"]),
                              Decimal(row["price"]), source=row.get("source") or "import",
                              entered_by=getpass.getuser())
            dates.append(_d(row["as_of_date"]))
        _remark(s, dates)
    print(f"Imported {len(dates)} marks")


def cmd_positions(args) -> None:
    with session_scope() as s:
        lines = services.positions(s, _d(args.date))
    print(f"{'book':8} {'instrument':28} {'qty':>8} {'avg':>10} {'mark':>10} {'unrealized':>14}")
    for ln in lines:
        if ln.quantity == 0:
            continue
        print(f"{ln.book:8} {ln.instrument_id:28} {number(ln.quantity):>8} "
              f"{price(ln.avg_price):>10} {price(ln.mark):>10} {money(ln.unrealized):>14}")


def cmd_pnl(args) -> None:
    with session_scope() as s:
        r = services.run_daily_pnl(s, _d(args.date), _d(args.prior) if args.prior else None,
                                   store=not args.no_store)
    print(f"Daily P&L {r.as_of} vs {r.prior}")
    print(f"{'book':8} {'instrument':28} {'daily':>12} {'new trd':>12} {'px move':>12} "
          f"{'fees':>10} {'amends':>10} {'cumulative':>14}")
    for ln in r.lines:
        print(f"{ln.book:8} {ln.instrument_id:28} {money(ln.daily):>12} "
              f"{money(ln.new_trades):>12} {money(ln.price_move):>12} "
              f"{money(ln.fees_today):>10} {money(ln.amendments):>10} "
              f"{money(ln.cumulative):>14}")
    print(f"{'TOTAL':37} {money(r.total_daily):>12} {'':>47} {money(r.total_cumulative):>14}")
    for e in r.errors:
        print(f"WARNING: {e}", file=sys.stderr)
    if r.errors:
        print("Not stored: fix the warnings and run again.", file=sys.stderr)
        sys.exit(1)


def cmd_agents_list(args) -> None:
    for a in agents.all_agents():
        print(f"{a.name:12} {a.description}")


def _print_runs(runs) -> None:
    for r in runs:
        print(f"{r.agent:12} {r.status:8} {r.message}")


def _print_load(loaded) -> None:
    _print_runs(loaded.runs)
    days = loaded.dates_marked
    span = str(days[0]) if len(days) == 1 else f"{days[0]} to {days[-1]} ({len(days)} days)"
    print(f"== Marks ({span})")
    _print_marks(loaded.marks)


def cmd_agents_run(args) -> None:
    """Load prices, then mark every contract for each day that got new prices."""
    with session_scope() as s:
        loaded = marking.load_prices(s, _d(args.date), only=args.only,
                                     backfill_days=args.backfill)
        _print_load(loaded)
    if any(r.status == "failed" for r in loaded.runs):
        sys.exit(1)


def _print_marks(results) -> None:
    for r in results:
        print(f"{r.instrument_id:40} {r.status:12} {price(r.price):>10}  {r.method} {r.note}")


def cmd_mark_build(args) -> None:
    with session_scope() as s:
        results = marking.build_marks(s, _d(args.date))
        _print_marks(results)
    if any(r.status == "no-source" for r in results):
        sys.exit(1)


def cmd_eod(args) -> None:
    """Run the agents, build marks, then run the daily P&L."""
    with session_scope() as s:
        print("== Agents")
        _print_load(marking.load_prices(s, _d(args.date), backfill_days=args.backfill))
    print("== P&L")
    args.prior, args.no_store = None, False
    cmd_pnl(args)


def cmd_proxy_list(args) -> None:
    with session_scope() as s:
        rules = marking.list_proxies(s)
        if not rules:
            print("No proxy rules.")
        for r in rules:
            print(f"{r.id:>4}  {r.target:24} <- {r.source_kind} {r.source}"
                  f"{adjustment(r.multiplier, r.spread)}  priority {r.priority}")


def cmd_proxy_add(args) -> None:
    with session_scope() as s:
        r = marking.add_proxy(s, args.target, args.source, args.multiplier, args.spread,
                              args.priority, args.comment, getpass.getuser())
        print(f"Proxy rule {r.id}: {r.target} marked from {r.source_kind} {r.source} "
              "when it has no price of its own")


def cmd_proxy_remove(args) -> None:
    with session_scope() as s:
        marking.remove_proxy(s, args.rule_id)
    print(f"Removed proxy rule {args.rule_id}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ctrade", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="create tables and load products").set_defaults(fn=cmd_init)
    sub.add_parser("products", help="list products").set_defaults(fn=cmd_products)

    inst = sub.add_parser("instrument", help="create an instrument")
    inst_sub = inst.add_subparsers(dest="kind", required=True)
    fut = inst_sub.add_parser("future")
    fut.add_argument("product")
    fut.add_argument("month", help="YYYY-MM")
    spot = inst_sub.add_parser("spot")
    spot.add_argument("gpu")
    spot.add_argument("start")
    spot.add_argument("end")
    spot.add_argument("--venue", default="OTC")
    inst.set_defaults(fn=cmd_instrument)
    months = inst_sub.add_parser("months", help="create consecutive monthly contracts")
    months.add_argument("products", nargs="+")
    months.add_argument("--first", default=date.today().strftime("%Y-%m"), help="YYYY-MM")
    months.add_argument("--count", type=int, default=12)
    months.set_defaults(fn=cmd_instrument_months)

    trade = sub.add_parser("trade", help="book, amend, cancel or import trades")
    tsub = trade.add_subparsers(dest="action", required=True)
    add = tsub.add_parser("add")
    add.add_argument("--instrument")
    add.add_argument("--product")
    add.add_argument("--month")
    add.add_argument("--side", required=True, choices=["buy", "sell"])
    add.add_argument("--qty", required=True)
    add.add_argument("--price", required=True, help="$ per GPU-hour")
    add.add_argument("--date", default=date.today().isoformat())
    add.add_argument("--book", default="MAIN")
    add.add_argument("--fees", default="0")
    add.add_argument("--cpty", default="")
    add.add_argument("--ref", default="")
    add.add_argument("--comment", default="")
    add.set_defaults(fn=cmd_trade_add)
    amend = tsub.add_parser("amend")
    amend.add_argument("trade_id", type=int)
    for opt in ("qty", "price", "fees", "date", "side", "book", "comment"):
        amend.add_argument(f"--{opt}")
    amend.set_defaults(fn=cmd_trade_amend)
    cancel = tsub.add_parser("cancel")
    cancel.add_argument("trade_id", type=int)
    cancel.add_argument("--comment", default="")
    cancel.set_defaults(fn=cmd_trade_cancel)
    imp = tsub.add_parser("import")
    imp.add_argument("path")
    imp.set_defaults(fn=cmd_trade_import)

    sub.add_parser("trades", help="list live trades").set_defaults(fn=cmd_trades)

    mark = sub.add_parser("mark", help="enter or import marks")
    msub = mark.add_subparsers(dest="action", required=True)
    mset = msub.add_parser("set")
    mset.add_argument("instrument")
    mset.add_argument("date")
    mset.add_argument("price")
    mset.add_argument("--source", default="manual")
    mset.add_argument("--comment", default="")
    mset.set_defaults(fn=cmd_mark_set)
    mimp = msub.add_parser("import")
    mimp.add_argument("path")
    mimp.set_defaults(fn=cmd_mark_import)
    mbuild = msub.add_parser("build", help="mark traded instruments from agent data")
    mbuild.add_argument("--date", default=date.today().isoformat())
    mbuild.set_defaults(fn=cmd_mark_build)

    ag = sub.add_parser("agents", help="market data agents")
    agsub = ag.add_subparsers(dest="action", required=True)
    agsub.add_parser("list").set_defaults(fn=cmd_agents_list)
    agrun = agsub.add_parser("run", help="collect prices from public sources")
    agrun.add_argument("--date", default=date.today().isoformat())
    agrun.add_argument("--only", nargs="+", help="agent names, e.g. ornn web")
    agrun.add_argument("--backfill", type=int, default=0, help="days of history to fetch")
    agrun.set_defaults(fn=cmd_agents_run)

    px = sub.add_parser("proxy", help="proxy marks: mark one contract off another")
    pxsub = px.add_subparsers(dest="action", required=True)
    pxsub.add_parser("list").set_defaults(fn=cmd_proxy_list)
    pxadd = pxsub.add_parser("add", help="e.g. proxy add NODAL.H100 CME.GPU1")
    pxadd.add_argument("target", help="product code or instrument id to mark")
    pxadd.add_argument("source", help="product code, instrument id, or series:<name>")
    pxadd.add_argument("--multiplier", default="1")
    pxadd.add_argument("--spread", default="0", help="$ per GPU-hour added to the source")
    pxadd.add_argument("--priority", type=int, default=1, help="lower is tried first")
    pxadd.add_argument("--comment", default="")
    pxadd.set_defaults(fn=cmd_proxy_add)
    pxrm = pxsub.add_parser("remove")
    pxrm.add_argument("rule_id", type=int)
    pxrm.set_defaults(fn=cmd_proxy_remove)

    eod = sub.add_parser("eod", help="agents, then marks, then daily P&L")
    eod.add_argument("--date", default=date.today().isoformat())
    eod.add_argument("--backfill", type=int, default=0)
    eod.set_defaults(fn=cmd_eod)

    pos = sub.add_parser("positions", help="positions and unrealized P&L")
    pos.add_argument("--date", default=date.today().isoformat())
    pos.set_defaults(fn=cmd_positions)

    pnl = sub.add_parser("pnl", help="run the daily P&L")
    pnl.add_argument("--date", default=date.today().isoformat())
    pnl.add_argument("--prior", help="defaults to the previous business day")
    pnl.add_argument("--no-store", action="store_true")
    pnl.set_defaults(fn=cmd_pnl)
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        # Checks the connection and adds any tables a newer version needs; data is kept.
        init_db()
        args.fn(args)
    except (ValueError, DatabaseUnavailable) as e:
        raise SystemExit(f"Error: {e}") from None


if __name__ == "__main__":
    main()

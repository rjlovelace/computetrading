"""Command-line interface: `ctrade --help`."""

from __future__ import annotations

import argparse
import csv
import getpass
import sys
from datetime import date
from decimal import Decimal

from sqlalchemy import select

from . import services
from .db import Product, init_db, session_scope


def _d(text: str) -> date:
    return date.fromisoformat(text)


def _money(x) -> str:
    return "" if x is None else f"{Decimal(x):,.2f}"


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
    print(f"Database ready; {n} products loaded.")


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
        print(f"{inst.instrument_id}  hours/contract={inst.hours_per_contract}  "
              f"last trade {inst.last_trade_date}")


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
        print(f"Booked trade {t.trade_id}: {t.side} {t.quantity} {t.instrument_id} @ {t.price}")


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
                  f"{t.quantity:>8} {t.instrument_id:28} @ {t.price}")


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


def cmd_mark_set(args) -> None:
    with session_scope() as s:
        services.set_mark(s, args.instrument, _d(args.date), Decimal(args.price),
                          source=args.source, comment=args.comment,
                          entered_by=getpass.getuser())
    print("Mark saved")


def cmd_mark_import(args) -> None:
    """CSV columns: instrument_id,as_of_date,price[,source]"""
    n = 0
    with session_scope() as s, open(args.path, newline="") as f:
        for row in csv.DictReader(f):
            services.set_mark(s, row["instrument_id"], _d(row["as_of_date"]),
                              Decimal(row["price"]), source=row.get("source") or "import",
                              entered_by=getpass.getuser())
            n += 1
    print(f"Imported {n} marks")


def cmd_positions(args) -> None:
    with session_scope() as s:
        lines = services.positions(s, _d(args.date))
    print(f"{'book':8} {'instrument':28} {'qty':>8} {'avg':>10} {'mark':>10} {'unrealized':>14}")
    for ln in lines:
        if ln.quantity == 0:
            continue
        print(f"{ln.book:8} {ln.instrument_id:28} {ln.quantity:>8} {ln.avg_price:>10.4f} "
              f"{'' if ln.mark is None else f'{ln.mark:.4f}':>10} {_money(ln.unrealized):>14}")


def cmd_pnl(args) -> None:
    with session_scope() as s:
        r = services.run_daily_pnl(s, _d(args.date), _d(args.prior) if args.prior else None,
                                   store=not args.no_store)
    print(f"Daily P&L {r.as_of} vs {r.prior}")
    print(f"{'book':8} {'instrument':28} {'daily':>12} {'new trd':>12} {'px move':>12} "
          f"{'fees':>10} {'amends':>10} {'cumulative':>14}")
    for ln in r.lines:
        print(f"{ln.book:8} {ln.instrument_id:28} {_money(ln.daily):>12} "
              f"{_money(ln.new_trades):>12} {_money(ln.price_move):>12} "
              f"{_money(ln.fees_today):>10} {_money(ln.amendments):>10} "
              f"{_money(ln.cumulative):>14}")
    print(f"{'TOTAL':37} {_money(r.total_daily):>12} {'':>47} {_money(r.total_cumulative):>14}")
    for e in r.errors:
        print(f"WARNING: {e}", file=sys.stderr)
    if r.errors:
        print("Not stored: fix the warnings and run again.", file=sys.stderr)
        sys.exit(1)


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
        args.fn(args)
    except ValueError as e:
        raise SystemExit(f"Error: {e}") from None


if __name__ == "__main__":
    main()

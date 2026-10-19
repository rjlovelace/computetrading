from datetime import date
from decimal import Decimal as D

import pytest

from ctrade import marking, services
from ctrade.db import Mark, MarketObservation, session_scope

H100 = "ORNN.OCPI.H100 SXM"
NOV3 = date(2026, 11, 3)


def obs(s, series, d, value):
    s.add(MarketObservation(series=series, obs_date=d, value=D(value), agent="test"))


def setup(s, *instruments):
    ids = []
    for inst in instruments:
        services.book_trade(s, instrument_id=inst.instrument_id, side="buy", quantity=D(1),
                            price=D("2.000"), trade_date=date(2026, 10, 30))
        ids.append(inst.instrument_id)
    obs(s, H100, date(2026, 10, 30), "2.40")
    obs(s, H100, date(2026, 11, 2), "2.50")
    obs(s, H100, NOV3, "2.60")
    s.flush()
    return ids


def results(s):
    return {r.instrument_id: r for r in marking.build_marks(s, NOV3)}


def test_current_month_uses_expected_average(url):
    with session_scope(url) as s:
        (iid,) = setup(s, services.ensure_future(s, "ICE.HPR", "2026-11"))
        r = results(s)[iid]
        # Nov 1 carries Oct 30 (2.40), Nov 2 = 2.50, Nov 3 = 2.60, 27 days at 2.60
        assert r.price == D("2.590000")
        assert r.method == "index-average"
        assert "2 of 30 days published" in r.note
        assert services.mark_map(s, [NOV3])[(iid, NOV3)] == D("2.59")


def test_future_month_is_flat_forward(url):
    with session_scope(url) as s:
        (iid,) = setup(s, services.ensure_future(s, "ICE.HPR", "2026-12"))
        r = results(s)[iid]
        assert (r.price, r.method) == (D("2.60"), "index-flat-forward")


def test_settlement_beats_index(url):
    with session_scope(url) as s:
        (iid,) = setup(s, services.ensure_future(s, "ICE.HPR", "2026-11"))
        obs(s, "SETTLE." + iid, NOV3, "2.55")
        assert results(s)[iid].price == D("2.55")


def test_manual_mark_is_never_replaced(url):
    with session_scope(url) as s:
        (iid,) = setup(s, services.ensure_future(s, "ICE.HPR", "2026-11"))
        services.set_mark(s, iid, NOV3, D("2.70"))
        r = results(s)[iid]
        assert r.status == "kept-manual"
        assert s.query(Mark).count() == 1


def test_rerun_is_idempotent(url):
    with session_scope(url) as s:
        (iid,) = setup(s, services.ensure_future(s, "ICE.HPR", "2026-11"))
        results(s)
        assert results(s)[iid].status == "unchanged"
        assert s.query(Mark).count() == 1


def test_no_proxy_unless_configured(url):
    with session_scope(url) as s:
        (iid,) = setup(s, services.ensure_future(s, "CME.GPU1", "2026-11"))
        r = results(s)[iid]
        assert r.status == "no-source" and "SILICONDATA.H100" in r.note


SD = "SILICONDATA.H100"


def test_product_proxy_uses_source_contract_for_same_month(url):
    with session_scope(url) as s:
        nodal = services.ensure_future(s, "NODAL.H100", "2026-12")
        setup(s, nodal)
        obs(s, SD, NOV3, "2.20")
        marking.add_proxy(s, "NODAL.H100", "CME.GPU1")
        r = results(s)[nodal.instrument_id]
        assert (r.price, r.method) == (D("2.20"), "proxy-index-flat-forward")
        assert "proxy CME.GPU1" in r.note


def test_proxy_uses_source_contracts_manual_mark_and_adjustment(url):
    with session_scope(url) as s:
        nodal = services.ensure_future(s, "NODAL.H100", "2026-12")
        gpu1 = services.ensure_future(s, "CME.GPU1", "2026-12")
        setup(s, nodal)
        services.set_mark(s, gpu1.instrument_id, NOV3, D("2.30"))
        marking.add_proxy(s, "NODAL.H100", "CME.GPU1", multiplier="1", spread="-0.05")
        r = results(s)[nodal.instrument_id]
        assert r.price == D("2.25") and r.method == "proxy-manual"


def test_series_proxy_and_priority_order(url):
    with session_scope(url) as s:
        nodal = services.ensure_future(s, "NODAL.H100", "2026-12")
        setup(s, nodal)
        marking.add_proxy(s, "NODAL.H100", "series:" + SD, priority=1)  # no data: skipped
        marking.add_proxy(s, "NODAL.H100", "series:" + H100, priority=2)
        r = results(s)[nodal.instrument_id]
        assert r.price == D("2.60") and f"no {SD} value" not in r.note


def test_own_price_beats_proxy(url):
    with session_scope(url) as s:
        (iid,) = setup(s, services.ensure_future(s, "ICE.HPR", "2026-12"))
        obs(s, SD, NOV3, "9.99")
        marking.add_proxy(s, "ICE.HPR", "series:" + SD)
        assert results(s)[iid].method == "index-flat-forward"


def test_circular_proxies_give_no_mark(url):
    with session_scope(url) as s:
        a = services.ensure_future(s, "NODAL.H100", "2026-12")
        services.ensure_future(s, "NODAL.B200", "2026-12")
        setup(s, a)
        marking.add_proxy(s, "NODAL.H100", "NODAL.B200")
        marking.add_proxy(s, "NODAL.B200", "NODAL.H100")
        r = results(s)[a.instrument_id]
        assert r.status == "no-source" and "circular" in r.note


def test_bad_proxy_rules_are_refused(url):
    with session_scope(url) as s:
        for target, source in [("NOPE", "CME.GPU1"), ("NODAL.H100", "NOPE"),
                               ("NODAL.H100", "NODAL.H100"), ("NODAL.H100", "series:")]:
            with pytest.raises(ValueError):
                marking.add_proxy(s, target, source)


def test_untraded_contracts_are_marked_too(url):
    with session_scope(url) as s:
        setup(s)
        inst = services.ensure_future(s, "ICE.HPR", "2027-01")
        assert results(s)[inst.instrument_id].price == D("2.60")


def test_loading_prices_marks_each_new_day(url):
    class FakeAgent:
        name, description = "fake", "test prices"

        def collect(self, as_of, backfill_days=0):
            from ctrade.agents import Observation
            return [Observation(H100, date(2026, 11, 2), D("2.50")),
                    Observation(H100, NOV3, D("2.60"))]

    with session_scope(url) as s:
        inst = services.ensure_future(s, "ICE.HPR", "2026-12")
        loaded = marking.load_prices(s, NOV3, agents=[FakeAgent()])
        assert loaded.dates_marked == [date(2026, 11, 2), NOV3]
        history = services.mark_history(s, [inst.instrument_id])
        assert [(m.as_of_date, m.price) for m in history] == [
            (date(2026, 11, 2), D("2.50")), (NOV3, D("2.60"))]


def test_stale_index_is_not_used(url):
    with session_scope(url) as s:
        inst = services.ensure_future(s, "ICE.BKL", "2026-11")
        services.book_trade(s, instrument_id=inst.instrument_id, side="buy", quantity=D(1),
                            price=D("7.000"), trade_date=date(2026, 10, 20))
        obs(s, "ORNN.OCPI.B200", date(2026, 10, 20), "7.10")
        s.flush()
        assert results(s)[inst.instrument_id].status == "no-source"


def test_spot_block_uses_latest_index(url):
    with session_scope(url) as s:
        spot = services.ensure_spot(s, "H100", date(2026, 11, 1), date(2026, 11, 30))
        (iid,) = setup(s, spot)
        r = results(s)[iid]
        assert (r.price, r.method) == (D("2.60"), "index-spot")


def test_marks_feed_the_daily_pnl(url):
    with session_scope(url) as s:
        (iid,) = setup(s, services.ensure_future(s, "ICE.HPR", "2026-11"))
        results(s)
        (ln,) = services.positions(s, NOV3)
        assert ln.mark == D("2.59")
        assert ln.unrealized == (D("2.59") - D("2.000")) * 720

from datetime import date
from decimal import Decimal as D

import httpx
import pytest

from ctrade.agents import AgentSkipped, run_agents
from ctrade.agents.ornn import OrnnAgent
from ctrade.agents.settlements import SettlementFileAgent
from ctrade.agents.web_reader import Extraction, WebReaderAgent, WebSource, validate
from ctrade.db import AgentRun, MarketObservation

# Response shapes copied from Ornn's API documentation.
ORNN_ALL = {
    "success": True,
    "date": "2026-11-02T20:00:00.000Z",
    "data": [
        {"gpu_type": "H100 SXM", "region": "global", "index_value": 2.505,
         "date": "2026-11-02T20:00:00.000Z"},
        {"gpu_type": "B200", "region": "global", "index_value": 7.27625,
         "date": "2026-11-02T20:00:00.000Z"},
    ],
}
ORNN_HISTORY = {
    "success": True,
    "gpu_type": "H100 SXM",
    "data": [
        {"timestamp": "2026-10-30T20:00:00.000Z", "index_value": 2.48},
        {"timestamp": "2026-10-31T20:00:00.000Z", "index_value": 2.49},
    ],
}


def ornn_client():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/daily-index/all":
            return httpx.Response(200, json=ORNN_ALL)
        if request.url.path.endswith("/index-history"):
            return httpx.Response(200, json=ORNN_HISTORY)
        return httpx.Response(404)

    return httpx.Client(base_url="https://api.ornnai.com", transport=httpx.MockTransport(handler))


def test_ornn_latest_values():
    obs = OrnnAgent(client=ornn_client()).collect(date(2026, 11, 2))
    by_series = {o.series: o for o in obs}
    assert by_series["ORNN.OCPI.H100 SXM"].value == D("2.505")
    assert by_series["ORNN.OCPI.B200"].obs_date == date(2026, 11, 2)


def test_ornn_backfill_adds_history():
    obs = OrnnAgent(client=ornn_client()).collect(date(2026, 11, 2), backfill_days=5)
    dates = sorted(o.obs_date for o in obs if o.series == "ORNN.OCPI.H100 SXM")
    assert date(2026, 10, 30) in dates and date(2026, 11, 2) in dates


def test_ornn_ignores_values_after_run_date():
    assert OrnnAgent(client=ornn_client()).collect(date(2026, 11, 1)) == []


SOURCE = WebSource("SILICONDATA.H100", "Silicon Data H100", "https://example.com/h100",
                   D("0.5"), D("20"))
PAGE = "H100 Rental Price Index 2.83 USD / GPU-hour As of Oct 18, 2026 7D +7.6%"


def ext(value=2.83, quote="2.83 USD / GPU-hour As of Oct 18, 2026", day="2026-10-18"):
    return Extraction(found=True, value=value, as_of_date=day, quote=quote)


def test_web_extraction_accepted_when_quote_matches():
    obs = validate(SOURCE, PAGE, ext(), date(2026, 10, 18))
    assert obs.value == D("2.83") and obs.obs_date == date(2026, 10, 18)
    assert obs.method == "web-extract"


@pytest.mark.parametrize("bad, reason", [
    (ext(quote="2.91 USD / GPU-hour"), "does not appear"),
    (ext(value=2.53), "not in the quoted text"),
    (ext(day="2026-10-19"), "after the run date"),
    (Extraction(found=False, value=None, as_of_date=None, quote=""), "no current value"),
])
def test_web_extraction_rejected(bad, reason):
    with pytest.raises(ValueError, match=reason):
        validate(SOURCE, PAGE, bad, date(2026, 10, 18))


def test_web_extraction_rejects_out_of_range_value():
    src = WebSource(SOURCE.series, SOURCE.name, SOURCE.url, D("5"), D("20"))
    with pytest.raises(ValueError, match="sanity range"):
        validate(src, PAGE, ext(), date(2026, 10, 18))


def test_web_agent_reads_page_with_extractor():
    html = f"<html><script>var x=1</script><body><h1>{PAGE}</h1></body></html>"
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=html)))
    seen = {}

    def fake_extractor(source, text):
        seen["text"] = text
        return ext()

    agent = WebReaderAgent([SOURCE], client=client, extractor=fake_extractor)
    (obs,) = agent.collect(date(2026, 10, 18))
    assert obs.value == D("2.83")
    assert "var x" not in seen["text"]


def test_web_agent_skips_without_credentials(monkeypatch, tmp_path):
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(AgentSkipped):
        WebReaderAgent([SOURCE]).collect(date(2026, 10, 18))


def test_settlement_files(tmp_path):
    (tmp_path / "ice.csv").write_text(
        "instrument_id,date,settle\nICE.HPR.2026-12,2026-11-02,2.415\n"
        "ICE.HPR.2026-12,2026-11-03,2.420\n"
    )
    (obs,) = SettlementFileAgent(tmp_path).collect(date(2026, 11, 2))
    assert obs.series == "SETTLE.ICE.HPR.2026-12" and obs.value == D("2.415")


class BrokenAgent:
    name = "broken"
    description = "always fails"

    def collect(self, as_of, backfill_days=0):
        raise RuntimeError("source down")


def test_runner_isolates_failures_and_dedupes(url):
    from ctrade.db import session_scope

    with session_scope(url) as s:
        agents = [BrokenAgent(), OrnnAgent(client=ornn_client())]
        runs = run_agents(s, date(2026, 11, 2), agents=agents)
        assert [r.status for r in runs] == ["failed", "ok"]
        assert "source down" in runs[0].message
        assert runs[1].observations == 2
        again = run_agents(s, date(2026, 11, 2), agents=[OrnnAgent(client=ornn_client())])
        assert again[0].observations == 0  # unchanged values aren't stored twice
        assert s.query(MarketObservation).count() == 2
        assert s.query(AgentRun).count() == 3

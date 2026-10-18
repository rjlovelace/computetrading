"""Web-reader agent: uses Claude to read public index pages that have no free API.

Each page in config/web_sources.toml is fetched, reduced to text, and given to Claude, which
returns the headline index value, its as-of date and the exact sentence it came from. The
agent keeps a value only when that quote appears verbatim on the page, contains the number,
and the number is inside the page's [min, max] range. Needs ANTHROPIC_API_KEY (or another
Anthropic credential); without one the agent is skipped.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path

import httpx
from pydantic import BaseModel

from .base import USER_AGENT, AgentSkipped, Observation

MODEL = "claude-opus-5-5"
DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "web_sources.toml"
MAX_PAGE_CHARS = 60_000


@dataclass(frozen=True)
class WebSource:
    series: str
    name: str
    url: str
    min: Decimal
    max: Decimal


class Extraction(BaseModel):
    found: bool
    value: float | None
    as_of_date: str | None  # ISO YYYY-MM-DD
    quote: str  # exact text from the page containing the value


Extractor = Callable[[WebSource, str], Extraction]


def load_sources(path: Path = DEFAULT_CONFIG) -> list[WebSource]:
    if not path.exists():
        return []
    with open(path, "rb") as f:
        pages = tomllib.load(f).get("page", [])
    return [
        WebSource(p["series"], p["name"], p["url"], Decimal(str(p["min"])), Decimal(str(p["max"])))
        for p in pages
    ]


class _TextExtractor(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return re.sub(r"\s+", " ", " ".join(parser.parts))


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def claude_extractor() -> Extractor:
    import anthropic

    client = anthropic.Anthropic()

    def extract(source: WebSource, page_text: str) -> Extraction:
        prompt = (
            f"This is the text of the public web page for the {source.name}, fetched from "
            f"{source.url}.\n\n<page>\n{page_text[:MAX_PAGE_CHARS]}\n</page>\n\n"
            "Find the headline current value of this index in USD per GPU-hour and the date "
            "the page says that value is as of. Set quote to the shortest exact passage from "
            "the page that contains the number, copied character for character. If the page "
            "shows no clear current value with a date, set found to false and leave value and "
            "as_of_date null. Do not compute, estimate or use FAQ or historical figures."
        )
        response = client.beta.messages.parse(
            model=MODEL,
            max_tokens=2000,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
            output_format=Extraction,
        )
        if response.stop_reason == "refusal" or response.parsed_output is None:
            return Extraction(found=False, value=None, as_of_date=None, quote="")
        return response.parsed_output

    return extract


def validate(source: WebSource, page_text: str, ext: Extraction, as_of: date) -> Observation:
    """Turn an extraction into an observation, or raise ValueError saying why it was rejected."""
    if not ext.found or ext.value is None or not ext.as_of_date:
        raise ValueError("no current value with a date found on the page")
    try:
        value = Decimal(str(ext.value))
        obs_date = date.fromisoformat(ext.as_of_date)
    except (InvalidOperation, ValueError) as e:
        raise ValueError(f"unreadable value or date: {e}") from None
    if not ext.quote or _normalise(ext.quote) not in _normalise(page_text):
        raise ValueError("quoted text does not appear on the page")
    numbers = {Decimal(n) for n in re.findall(r"\d+(?:\.\d+)?", ext.quote.replace(",", ""))}
    if value not in numbers:
        raise ValueError(f"value {value} is not in the quoted text")
    if not source.min <= value <= source.max:
        raise ValueError(f"value {value} outside sanity range {source.min}-{source.max}")
    if obs_date > as_of:
        raise ValueError(f"as-of date {obs_date} is after the run date {as_of}")
    return Observation(
        series=source.series,
        obs_date=obs_date,
        value=value,
        method="web-extract",
        source_url=source.url,
        detail=f'Quoted: "{ext.quote}"',
    )


class WebReaderAgent:
    name = "web"
    description = "Claude reads public index pages listed in config/web_sources.toml"

    def __init__(
        self,
        sources: list[WebSource] | None = None,
        client: httpx.Client | None = None,
        extractor: Extractor | None = None,
    ):
        self.sources = sources if sources is not None else load_sources()
        self.client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=30, follow_redirects=True
        )
        self._extractor = extractor
        self.rejections: list[str] = []

    def _get_extractor(self) -> Extractor:
        if self._extractor is None:
            has_credential = any(
                os.environ.get(k)
                for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")
            ) or Path.home().joinpath(".config", "anthropic").exists()
            if not has_credential:
                raise AgentSkipped("no Anthropic credential (set ANTHROPIC_API_KEY)")
            self._extractor = claude_extractor()
        return self._extractor

    def collect(self, as_of: date, backfill_days: int = 0) -> list[Observation]:
        if not self.sources:
            raise AgentSkipped("no pages configured in config/web_sources.toml")
        extractor = self._get_extractor()
        out: list[Observation] = []
        self.rejections = []
        for source in self.sources:
            try:
                resp = self.client.get(source.url)
                resp.raise_for_status()
                text = html_to_text(resp.text)
                out.append(validate(source, text, extractor(source, text), as_of))
            except (httpx.HTTPError, ValueError) as e:
                self.rejections.append(f"{source.series}: {e}")
        return out

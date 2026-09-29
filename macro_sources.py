"""Fetch and normalize the macro inputs behind the gold outlook.

Parsers are pure (payload in, dataclasses out) so the scoring model can be
tested without the network. Fetchers raise ``SourceError`` with a short reason.
"""

import csv
import html
import io
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time as clock_time, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import requests

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = (4, 10)

KALSHI_EVENTS_URL = "https://api.elections.kalshi.com/trade-api/v2/events"
POLYMARKET_EVENTS_URL = "https://gamma-api.polymarket.com/events"
WSCN_CALENDAR_URL = "https://api-one-wscn.awtmt.com/apiv1/finance/macrodatas"
WSCN_LIVES_URL = "https://api-one-wscn.awtmt.com/apiv1/content/lives"
WSCN_SEARCH_URL = "https://api-one-wscn.awtmt.com/apiv1/search/live"
FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"
SINA_DXY_URL = (
    "https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/"
    "var%20_DINIW=/NewForexService.getDayKLine"
)
FOREXFACTORY_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

_NEW_YORK = ZoneInfo("America/New_York")

# Decision day of each scheduled FOMC meeting; the statement is released at
# 14:00 New York time (federalreserve.gov/monetarypolicy/fomccalendars.htm).
FOMC_DECISION_DAYS = (
    (2026, 1, 28), (2026, 3, 18), (2026, 4, 29), (2026, 6, 17),
    (2026, 7, 29), (2026, 9, 16), (2026, 10, 28), (2026, 12, 9),
    (2027, 1, 27), (2027, 3, 17), (2027, 4, 28), (2027, 6, 9),
    (2027, 7, 28), (2027, 9, 15), (2027, 10, 27), (2027, 12, 8),
)

HttpGet = Callable[..., Any]


class SourceError(Exception):
    """A source is unreachable or returned something we cannot trust."""


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class FedOutcome:
    bps: int                      # -50 / -25 / 0 / +25 / +50 at the next meeting
    prob: float                   # normalized probability
    prev_prob: Optional[float] = None  # about 24 hours earlier, when the source has it


@dataclass
class FedExpectation:
    meeting_ts: float
    outcomes: List[FedOutcome]
    source: str
    as_of: float

    @property
    def expected_bps(self):
        # type: () -> float
        return sum(outcome.bps * outcome.prob for outcome in self.outcomes)

    @property
    def prev_expected_bps(self):
        # type: () -> Optional[float]
        if not self.outcomes or any(outcome.prev_prob is None for outcome in self.outcomes):
            return None
        return sum(outcome.bps * outcome.prev_prob for outcome in self.outcomes)

    def probability(self, kind):
        # type: (str) -> float
        if kind == "cut":
            return sum(o.prob for o in self.outcomes if o.bps < 0)
        if kind == "hike":
            return sum(o.prob for o in self.outcomes if o.bps > 0)
        return sum(o.prob for o in self.outcomes if o.bps == 0)

    @property
    def meeting_month(self):
        # type: () -> int
        return datetime.fromtimestamp(self.meeting_ts, _NEW_YORK).month

    def headline(self):
        # type: () -> str
        """Dominant scenario for the next meeting, e.g. ``10月加息 68%``."""
        names = {"cut": "降息", "hold": "按兵不动", "hike": "加息"}
        kind = max(("hike", "hold", "cut"), key=self.probability)
        return f"{self.meeting_month}月{names[kind]} {self.probability(kind) * 100:.0f}%"


@dataclass
class CalendarItem:
    id: str
    ts: float
    title: str
    event: str
    country: str
    importance: int
    kind: str                     # "data" (has numbers) or "event"
    unit: str = ""
    actual: Optional[float] = None
    forecast: Optional[float] = None
    previous: Optional[float] = None
    actual_text: str = ""
    forecast_text: str = ""
    previous_text: str = ""
    source: str = ""
    url: str = ""


@dataclass
class NewsItem:
    id: str
    ts: float
    text: str
    url: str = ""
    importance: int = 1
    source: str = ""


@dataclass
class Series:
    id: str
    points: List[Tuple[str, float]] = field(default_factory=list)  # ascending by date

    @property
    def last(self):
        # type: () -> Optional[Tuple[str, float]]
        return self.points[-1] if self.points else None

    def change(self, lookback):
        # type: (int) -> Optional[Tuple[float, float, str]]
        """(latest, change vs ``lookback`` observations earlier, latest date)."""
        if len(self.points) <= lookback:
            return None
        latest_date, latest = self.points[-1]
        return latest, latest - self.points[-1 - lookback][1], latest_date


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

_NUMBER = re.compile(r"[-+]?\d+(?:\.\d+)?")
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"\s+")


def parse_number(raw):
    # type: (Any) -> Optional[float]
    """Leading number in a quote/calendar field; units stay as the source wrote them."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
        return value if math.isfinite(value) else None
    text = str(raw).strip().replace(",", "").replace("，", "").replace("−", "-")
    match = _NUMBER.search(text)
    if not match:
        return None
    try:
        value = float(match.group())
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _text(raw, limit=400):
    # type: (Any, int) -> str
    if not isinstance(raw, str):
        return ""
    return _SPACES.sub(" ", html.unescape(_TAGS.sub("", raw))).strip()[:limit]


def _iso_timestamp(raw):
    # type: (Any) -> Optional[float]
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def fomc_decision_times():
    # type: () -> List[float]
    return [
        datetime.combine(date(*day), clock_time(14, 0), tzinfo=_NEW_YORK).timestamp()
        for day in FOMC_DECISION_DAYS
    ]


def next_fomc(now):
    # type: (float) -> Optional[float]
    upcoming = [ts for ts in fomc_decision_times() if ts > now]
    return min(upcoming) if upcoming else None


def _fomc_near(ts, tolerance=3 * 86400):
    # type: (float, float) -> Optional[float]
    close = [meeting for meeting in fomc_decision_times() if abs(meeting - ts) <= tolerance]
    return min(close, key=lambda meeting: abs(meeting - ts)) if close else None


def _fomc_in_month(year, month):
    # type: (int, int) -> Optional[float]
    for day, ts in zip(FOMC_DECISION_DAYS, fomc_decision_times()):
        if day[0] == year and day[1] == month:
            return ts
    return None


def _normalized(raw_outcomes):
    # type: (List[Tuple[int, Optional[float], Optional[float]]]) -> List[FedOutcome]
    usable = [(bps, prob, prev) for bps, prob, prev in raw_outcomes if prob is not None and prob >= 0]
    total = sum(prob for _, prob, _ in usable)
    if not usable or total <= 0.2:
        raise SourceError("利率概率不足")
    merged = {}  # type: Dict[int, List[float]]
    for bps, prob, prev in usable:
        slot = merged.setdefault(bps, [0.0, 0.0, 1.0])
        slot[0] += prob
        if prev is None or prev < 0:
            slot[2] = 0.0
        else:
            slot[1] += prev
    prev_total = sum(slot[1] for slot in merged.values())
    has_prev = all(slot[2] for slot in merged.values()) and prev_total > 0.2
    return [
        FedOutcome(
            bps=bps,
            prob=slot[0] / total,
            prev_prob=slot[1] / prev_total if has_prev else None,
        )
        for bps, slot in sorted(merged.items())
    ]


def _get(http_get, url, params=None, headers=None):
    # type: (HttpGet, str, Optional[dict], Optional[dict]) -> Any
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/plain, */*"}
    request_headers.update(headers or {})
    try:
        response = http_get(url, params=params, headers=request_headers, timeout=TIMEOUT)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SourceError(f"网络请求失败：{exc.__class__.__name__}") from exc
    return response


def _get_json(http_get, url, params=None, headers=None):
    # type: (HttpGet, str, Optional[dict], Optional[dict]) -> Any
    try:
        return _get(http_get, url, params, headers).json()
    except ValueError as exc:
        raise SourceError("返回内容不是 JSON") from exc


# --------------------------------------------------------------------------- #
# Fed expectations: Kalshi → Polymarket → CME FedWatch quoted by WallstreetCN
# --------------------------------------------------------------------------- #

_KALSHI_SUFFIX = re.compile(r"-([CH])(\d+)$")
_KALSHI_TITLE = re.compile(r"(cut|hike)\s*(>)?\s*(\d+)\s*bps", re.IGNORECASE)


def _step_bps(amount, above):
    # type: (int, bool) -> int
    if above:
        return (amount // 25) * 25 + 25
    return int(round(amount / 25.0)) * 25


def _kalshi_bps(market):
    # type: (dict) -> Optional[int]
    match = _KALSHI_SUFFIX.search(str(market.get("ticker", "")))
    if match:
        sign = -1 if match.group(1) == "C" else 1
        amount = int(match.group(2))
        # Kalshi encodes ">25bps" as 26.
        return sign * _step_bps(amount - 1, True) if amount % 25 == 1 else sign * _step_bps(amount, False)
    title = str(market.get("yes_sub_title") or market.get("subtitle") or "")
    if "maintain" in title.lower():
        return 0
    match = _KALSHI_TITLE.search(title)
    if not match:
        return None
    sign = -1 if match.group(1).lower() == "cut" else 1
    return sign * _step_bps(int(match.group(3)), bool(match.group(2)))


def _kalshi_value(market, name):
    # type: (dict, str) -> Optional[float]
    value = parse_number(market.get(f"{name}_dollars"))
    if value is None:
        cents = parse_number(market.get(name))
        value = cents / 100.0 if cents is not None else None
    return value if value is not None and 0 <= value <= 1 else None


def _market_prob(bid, ask, last):
    # type: (Optional[float], Optional[float], Optional[float]) -> Optional[float]
    """Tight two-sided quotes beat a possibly old last trade."""
    if bid is not None and ask is not None and 0 <= bid <= ask <= 1 and ask > 0 and ask - bid <= 0.1:
        return (bid + ask) / 2
    return last


def parse_kalshi_events(payload, now):
    # type: (Any, float) -> FedExpectation
    events = payload.get("events") if isinstance(payload, dict) else None
    if not isinstance(events, list):
        raise SourceError("Kalshi 返回格式异常")
    best = None
    for event in events:
        markets = event.get("markets") if isinstance(event, dict) else None
        if not isinstance(markets, list):
            continue
        closes = [_iso_timestamp(m.get("close_time")) for m in markets if isinstance(m, dict)]
        closes = [ts for ts in closes if ts is not None and ts > now]
        if closes and (best is None or min(closes) < best[0]):
            best = (min(closes), event, markets)
    if best is None:
        raise SourceError("Kalshi 暂无未结算的议息市场")

    close_ts, event, markets = best
    raw = []
    for market in markets:
        if not isinstance(market, dict) or market.get("status") not in (None, "active", "open"):
            continue
        bps = _kalshi_bps(market)
        if bps is None:
            continue
        prob = _market_prob(
            _kalshi_value(market, "yes_bid"), _kalshi_value(market, "yes_ask"),
            _kalshi_value(market, "last_price"),
        )
        prev = _market_prob(
            _kalshi_value(market, "previous_yes_bid"), _kalshi_value(market, "previous_yes_ask"),
            _kalshi_value(market, "previous_price"),
        )
        raw.append((bps, prob, prev))
    meeting = _fomc_near(_iso_timestamp(event.get("strike_date")) or close_ts) or close_ts
    return FedExpectation(meeting, _normalized(raw), "Kalshi", now)


_POLY_TITLE = re.compile(r"fed\s+decision\s+in\s+([a-z]+)", re.IGNORECASE)
_POLY_OUTCOME = re.compile(r"(\d+)\s*(\+)?\s*bps?\s*(decrease|cut|increase|hike)", re.IGNORECASE)
_MONTHS = {
    name: index for index, name in enumerate(
        ("january", "february", "march", "april", "may", "june", "july",
         "august", "september", "october", "november", "december"), start=1)
}


def _json_list(raw):
    # type: (Any) -> list
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return raw if isinstance(raw, list) else []


def _poly_bps(title):
    # type: (str) -> Optional[int]
    lowered = title.lower()
    if any(word in lowered for word in ("no change", "unchanged", "hold", "maintain")):
        return 0
    match = _POLY_OUTCOME.search(title)
    if not match:
        return None
    sign = -1 if match.group(3).lower() in ("decrease", "cut") else 1
    amount = int(match.group(1))
    return sign * (amount if match.group(2) is None else max(amount, 50))


def parse_polymarket_events(payload, now):
    # type: (Any, float) -> FedExpectation
    if not isinstance(payload, list):
        raise SourceError("Polymarket 返回格式异常")
    best = None
    for event in payload:
        if not isinstance(event, dict):
            continue
        match = _POLY_TITLE.search(str(event.get("title", "")))
        end_ts = _iso_timestamp(event.get("endDate"))
        month = _MONTHS.get(match.group(1).lower()) if match else None
        if not month or end_ts is None or end_ts <= now:
            continue
        end_year = datetime.fromtimestamp(end_ts, _NEW_YORK).year
        meeting = _fomc_in_month(end_year, month) or end_ts
        if meeting > now - 3600 and (best is None or meeting < best[0]):
            best = (meeting, event)
    if best is None:
        raise SourceError("Polymarket 暂无议息市场")

    meeting, event = best
    raw = []
    for market in event.get("markets") or []:
        if not isinstance(market, dict) or market.get("closed"):
            continue
        bps = _poly_bps(str(market.get("groupItemTitle") or market.get("question") or ""))
        if bps is None:
            continue
        prices = [parse_number(price) for price in _json_list(market.get("outcomePrices"))]
        last = prices[0] if prices and prices[0] is not None else parse_number(market.get("lastTradePrice"))
        prob = _market_prob(parse_number(market.get("bestBid")), parse_number(market.get("bestAsk")), last)
        day_change = parse_number(market.get("oneDayPriceChange"))
        if day_change is None and prob is not None and prob < 0.05:
            day_change = 0.0  # Polymarket omits the change for untraded tail outcomes.
        prev = prob - day_change if prob is not None and day_change is not None else None
        raw.append((bps, prob, prev))
    return FedExpectation(meeting, _normalized(raw), "Polymarket", now)


_FEDWATCH_SEGMENT = re.compile(r"美联储(?:到)?(\d{1,2})月(.*?)(?=美联储(?:到)?\d{1,2}月|$)", re.S)
_FEDWATCH_PROB = re.compile(r"(维持利率不变|(?:累计)?(加息|降息)(\d+)个基点)的概率为\s*([\d.]+)%")


def parse_fedwatch_text(text, posted_ts, now, max_age=3 * 86400):
    # type: (str, float, float, float) -> Optional[FedExpectation]
    """Read a ``CME美联储观察：...`` bulletin; only its nearest future meeting counts."""
    if "美联储观察" not in text or now - posted_ts > max_age:
        return None
    posted = datetime.fromtimestamp(posted_ts, _NEW_YORK)
    for segment in _FEDWATCH_SEGMENT.finditer(text):
        month = int(segment.group(1))
        year = posted.year + (1 if month < posted.month else 0)
        meeting = _fomc_in_month(year, month)
        if meeting is None:
            continue
        if meeting <= now:
            return None  # bulletin predates the meeting it describes
        raw = []
        for prob in _FEDWATCH_PROB.finditer(segment.group(2)):
            bps = 0 if prob.group(1) == "维持利率不变" else int(prob.group(3)) * (-1 if prob.group(2) == "降息" else 1)
            raw.append((bps, float(prob.group(4)) / 100.0, None))
        if 0.9 <= sum(p for _, p, _ in raw) <= 1.1:
            return FedExpectation(meeting, _normalized(raw), "CME FedWatch（华尔街见闻转述）", posted_ts)
        return None
    return None


def fetch_kalshi(http_get, now):
    # type: (HttpGet, float) -> FedExpectation
    payload = _get_json(http_get, KALSHI_EVENTS_URL, params={
        "series_ticker": "KXFEDDECISION", "status": "open", "with_nested_markets": "true",
    })
    return parse_kalshi_events(payload, now)


def fetch_polymarket(http_get, now):
    # type: (HttpGet, float) -> FedExpectation
    payload = _get_json(http_get, POLYMARKET_EVENTS_URL, params={
        "closed": "false", "tag_slug": "fed", "limit": "50",
    })
    return parse_polymarket_events(payload, now)


def fetch_fedwatch_bulletin(http_get, now):
    # type: (HttpGet, float) -> FedExpectation
    payload = _get_json(http_get, WSCN_SEARCH_URL, params={"query": "CME美联储观察", "limit": "10"},
                        headers={"Referer": "https://wallstreetcn.com/"})
    for item in sorted(parse_wscn_news(payload, "华尔街见闻"), key=lambda news: -news.ts):
        parsed = parse_fedwatch_text(item.text, item.ts, now)
        if parsed is not None:
            return parsed
    raise SourceError("近 3 日无 CME 美联储观察播报")


# --------------------------------------------------------------------------- #
# Calendar and news: WallstreetCN (reachable from mainland China)
# --------------------------------------------------------------------------- #

def _wscn_items(payload):
    # type: (Any) -> list
    data = payload.get("data") if isinstance(payload, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SourceError("华尔街见闻返回格式异常")
    return items


def parse_wscn_calendar(payload):
    # type: (Any) -> List[CalendarItem]
    result = []
    for raw in _wscn_items(payload):
        if not isinstance(raw, dict):
            continue
        ts = parse_number(raw.get("public_date"))
        title = _text(raw.get("title"), 120)
        if ts is None or not title:
            continue
        result.append(CalendarItem(
            id=f"wscn:{raw.get('id')}",
            ts=ts,
            title=title,
            event=_text(raw.get("event"), 80) or title,
            country=str(raw.get("country_id") or ""),
            importance=int(parse_number(raw.get("importance")) or 1),
            kind="data" if raw.get("calendar_type") == "FD" else "event",
            unit=_text(raw.get("unit"), 12),
            actual=parse_number(raw.get("actual")),
            forecast=parse_number(raw.get("forecast")),
            previous=parse_number(raw.get("previous")),
            actual_text=_text(raw.get("actual"), 20),
            forecast_text=_text(raw.get("forecast"), 20),
            previous_text=_text(raw.get("previous"), 20),
            source="华尔街见闻",
            url=str(raw.get("uri") or ""),
        ))
    return result


def parse_wscn_news(payload, source):
    # type: (Any, str) -> List[NewsItem]
    result = []
    for raw in _wscn_items(payload):
        if not isinstance(raw, dict):
            continue
        ts = parse_number(raw.get("display_time"))
        text = " ".join(part for part in (
            _text(raw.get("title"), 120), _text(raw.get("content_text") or raw.get("content"), 600),
        ) if part)
        if ts is None or not text:
            continue
        result.append(NewsItem(
            id=f"wscn:{raw.get('id')}",
            ts=ts,
            text=text,
            url=str(raw.get("uri") or ""),
            importance=int(parse_number(raw.get("score")) or 1),
            source=source,
        ))
    return result


def fetch_wscn_calendar(http_get, start, end):
    # type: (HttpGet, float, float) -> List[CalendarItem]
    payload = _get_json(http_get, WSCN_CALENDAR_URL, params={"start": int(start), "end": int(end)},
                        headers={"Referer": "https://wallstreetcn.com/"})
    return parse_wscn_calendar(payload)


def fetch_wscn_news(http_get):
    # type: (HttpGet) -> List[NewsItem]
    """Gold channel flashes plus the latest Fed-related search hits, de-duplicated."""
    referer = {"Referer": "https://wallstreetcn.com/"}
    items = {}  # type: Dict[str, NewsItem]
    errors = []
    for url, params in (
        (WSCN_LIVES_URL, {"channel": "goldc-channel", "limit": "60"}),
        (WSCN_SEARCH_URL, {"query": "美联储", "limit": "30"}),
    ):
        try:
            for item in parse_wscn_news(_get_json(http_get, url, params=params, headers=referer), "华尔街见闻"):
                items.setdefault(item.id, item)
        except SourceError as exc:
            errors.append(str(exc))
    if not items and errors:
        raise SourceError(errors[0])
    return sorted(items.values(), key=lambda item: -item.ts)


# --------------------------------------------------------------------------- #
# Rates and dollar: FRED daily series, Sina ICE dollar index K-line
# --------------------------------------------------------------------------- #

def parse_fred_csv(text, series_id):
    # type: (str, str) -> Series
    points = []
    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if len(row) < 2 or not re.match(r"^\d{4}-\d{2}-\d{2}$", row[0].strip()):
            continue
        value = parse_number(row[1]) if row[1].strip() not in ("", ".") else None
        if value is not None:
            points.append((row[0].strip(), value))
    if not points:
        raise SourceError(f"FRED {series_id} 无有效数据")
    return Series(series_id, points)


def fetch_fred(http_get, series_id, start_date):
    # type: (HttpGet, str, str) -> Series
    response = _get(http_get, FRED_CSV_URL, params={"id": series_id, "cosd": start_date},
                    headers={"Accept": "text/csv,*/*"})
    return parse_fred_csv(response.text, series_id)


def parse_sina_kline(text, series_id="DINIW", keep=60):
    # type: (str, str, int) -> Series
    match = re.search(r'\("([^"]*)"\)', text or "")
    if not match:
        raise SourceError("新浪行情返回格式异常")
    points = []
    for row in match.group(1).split("|"):
        fields = [part for part in row.split(",") if part]
        if len(fields) >= 5 and re.match(r"^\d{4}-\d{2}-\d{2}$", fields[0]):
            close = parse_number(fields[4])
            if close is not None and close > 0:
                points.append((fields[0], close))
    if not points:
        raise SourceError("新浪行情无有效数据")
    return Series(series_id, points[-keep:])


def fetch_sina_dxy(http_get):
    # type: (HttpGet) -> Series
    response = _get(http_get, SINA_DXY_URL, params={"symbol": "DINIW"},
                    headers={"Referer": "https://finance.sina.com.cn/"})
    return parse_sina_kline(response.text)


# --------------------------------------------------------------------------- #
# Fallback schedule: Forex Factory weekly calendar (no actual values)
# --------------------------------------------------------------------------- #

_FF_TITLES = {
    "Non-Farm Employment Change": "非农就业人口变动",
    "Unemployment Rate": "失业率",
    "Average Hourly Earnings m/m": "平均每小时工资环比",
    "CPI m/m": "CPI环比",
    "CPI y/y": "CPI同比",
    "Core CPI m/m": "核心CPI环比",
    "Core PCE Price Index m/m": "核心PCE物价指数环比",
    "ADP Non-Farm Employment Change": "ADP就业人数变动",
    "Unemployment Claims": "首次申请失业救济人数",
    "ISM Manufacturing PMI": "ISM制造业PMI",
    "ISM Services PMI": "ISM非制造业PMI",
    "Retail Sales m/m": "零售销售环比",
    "Federal Funds Rate": "美联储利率决议",
    "FOMC Statement": "FOMC声明",
    "JOLTS Job Openings": "JOLTS职位空缺",
}


def parse_forexfactory(payload):
    # type: (Any) -> List[CalendarItem]
    if not isinstance(payload, list):
        raise SourceError("Forex Factory 返回格式异常")
    result = []
    for raw in payload:
        if not isinstance(raw, dict) or raw.get("country") != "USD":
            continue
        impact = raw.get("impact")
        ts = _iso_timestamp(raw.get("date"))
        title = str(raw.get("title") or "").strip()
        if impact not in ("High", "Medium") or ts is None or not title:
            continue
        name = _FF_TITLES.get(title, title)
        result.append(CalendarItem(
            id=f"ff:{title}:{int(ts)}", ts=ts, title=name, event=name, country="US",
            importance=3 if impact == "High" else 2, kind="data",
            forecast=parse_number(raw.get("forecast")), previous=parse_number(raw.get("previous")),
            forecast_text=_text(raw.get("forecast"), 20), previous_text=_text(raw.get("previous"), 20),
            source="Forex Factory",
        ))
    return result


def fetch_forexfactory(http_get):
    # type: (HttpGet) -> List[CalendarItem]
    return parse_forexfactory(_get_json(http_get, FOREXFACTORY_URL))


def default_http_get(url, params=None, headers=None, timeout=TIMEOUT):
    return requests.get(url, params=params, headers=headers, timeout=timeout)


def safe_call(func, *args):
    # type: (Callable, Any) -> Tuple[Any, Optional[str]]
    """Run one fetcher; any failure becomes a short, user-readable reason."""
    try:
        return func(*args), None
    except SourceError as exc:
        return None, str(exc)
    except Exception as exc:  # malformed payloads must not break the outlook
        return None, f"解析失败：{exc.__class__.__name__}"


def first_available(attempts):
    # type: (Sequence[Tuple[str, Callable[[], Any]]]) -> Tuple[Any, List[str]]
    """Try sources in order; return the first result and the reasons for skips."""
    errors = []
    for name, call in attempts:
        result, error = safe_call(call)
        if error is None and result is not None:
            return result, errors
        errors.append(f"{name}: {error or '无数据'}")
    return None, errors

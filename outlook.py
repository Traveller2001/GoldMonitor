"""Gold outlook: a transparent factor scorecard built from macro inputs.

    score = 100 × Σ wᵢ·sᵢ,   Σ wᵢ = 1,   sᵢ ∈ [-1, 1] (positive = bullish gold)

Five factors, each explainable on its own line in the panel:

* 利率预期  next-FOMC pricing from prediction markets: the level of the expected
            move and its 24h change (hawkish pricing weighs on gold).
* 经济数据  surprises (actual − consensus) in US releases that move rate
            expectations, signed by their hawkish direction and decayed by age.
* 实际利率  5-day change of the 10-year TIPS yield (FRED, ~1 day lag).
* 美元      5-day change of the ICE dollar index.
* 新闻面    hawkish/dovish tone of Fed- and gold-related flashes in the last 24h,
            tagged by DeepSeek when configured, otherwise by a lexicon.

A factor with no usable source counts as 0 and lowers the confidence.
"""

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import macro_sources as src
from macro_sources import CalendarItem, FedExpectation, NewsItem, Series
from news_ai import DeepRead, NewsAnalyst, clip

FACTOR_SPECS = (
    ("fed", "利率预期", 0.35),
    ("data", "经济数据", 0.25),
    ("rates", "实际利率", 0.15),
    ("usd", "美元", 0.10),
    ("news", "新闻面", 0.15),
)

# WallstreetCN event name → (label, hawkish direction, surprise scale, weight, key release)
# direction +1: a higher print than expected is hawkish (bearish for gold).
INDICATORS = {
    "非农就业人口变动": ("非农", 1, 5.0, 1.0, True),
    "失业率": ("失业率", -1, 0.1, 0.8, True),
    "平均每小时工资环比": ("时薪环比", 1, 0.1, 0.5, True),
    "平均每小时工资同比": ("时薪同比", 1, 0.1, 0.4, False),
    "核心CPI环比": ("核心CPI环比", 1, 0.1, 1.0, True),
    "核心CPI同比": ("核心CPI同比", 1, 0.1, 0.7, True),
    "CPI环比": ("CPI环比", 1, 0.1, 0.6, True),
    "CPI同比": ("CPI同比", 1, 0.1, 0.5, True),
    "核心PCE物价指数环比": ("核心PCE环比", 1, 0.1, 0.8, True),
    "核心PCE物价指数同比": ("核心PCE同比", 1, 0.1, 0.6, True),
    "PCE物价指数环比": ("PCE环比", 1, 0.1, 0.4, False),
    "PCE物价指数同比": ("PCE同比", 1, 0.1, 0.4, False),
    "核心PPI环比": ("核心PPI环比", 1, 0.2, 0.3, False),
    "PPI环比": ("PPI环比", 1, 0.2, 0.3, False),
    "首次申请失业救济人数": ("初请失业金", -1, 1.5, 0.3, False),
    "ADP就业人数变动": ("ADP就业", 1, 4.0, 0.4, False),
    "JOLTS职位空缺": ("JOLTS职位空缺", 1, 25.0, 0.3, False),
    "ISM制造业PMI": ("ISM制造业", 1, 1.5, 0.4, False),
    "ISM非制造业PMI": ("ISM服务业", 1, 1.5, 0.4, False),
    "ISM服务业PMI": ("ISM服务业", 1, 1.5, 0.4, False),
    "零售销售环比": ("零售销售", 1, 0.4, 0.5, True),
    "核心零售销售环比": ("核心零售", 1, 0.4, 0.3, False),
    "实际GDP年化季环比": ("GDP", 1, 0.5, 0.4, False),
}

RELEASE_WINDOW = 10 * 86400
RELEASE_HALF_LIFE_HOURS = 72.0
NEWS_WINDOW = 24 * 3600
NEWS_HALF_LIFE_HOURS = 8.0
# FRED publishes daily; Sina's dollar index bar moves all session long.
SERIES_TTL = {"rates": 3600, "usd": 15 * 60}
SERIES_MAX_AGE = 36 * 3600
FOREXFACTORY_TTL = 3600
SOURCE_COOLDOWN = 15 * 60

# Automatic deep reads (on top of news_ai's minimum gaps between calls).
DEEP_SCHEDULE = 8 * 3600       # about once per trading session, only while a market is open
DEEP_RATE_SHIFT_BPS = 5.0
DEEP_SCORE_SHIFT = 20
DEEP_FLIP_MARGIN = 8           # a flip at a stance boundary must be a real move, not a point of noise
UNREAD_RELEASE_WINDOW = 6 * 3600


# --------------------------------------------------------------------------- #
# Result model
# --------------------------------------------------------------------------- #

@dataclass
class Factor:
    key: str
    name: str
    weight: float
    signal: Optional[float] = None
    value: str = ""
    note: str = ""

    @property
    def available(self):
        # type: () -> bool
        return self.signal is not None

    @property
    def points(self):
        # type: () -> float
        return 0.0 if self.signal is None else self.weight * self.signal * 100


@dataclass
class Release:
    item: CalendarItem
    label: str
    surprise: float
    impact: float
    weight: float
    key: bool

    def describe(self):
        # type: () -> str
        unit = self.item.unit if self.item.unit in ("%", "万人") else ""
        return f"{self.label} {self.item.actual_text}{unit}（预期 {self.item.forecast_text}{unit}）"

    @property
    def verdict(self):
        # type: () -> str
        if self.impact >= 0.25:
            return "利多黄金"
        if self.impact <= -0.25:
            return "利空黄金"
        return "符合预期"


@dataclass
class Headline:
    item: NewsItem
    tone: int
    by: str


@dataclass
class Outlook:
    generated_at: float
    score: Optional[int]
    stance: str
    tone: int
    confidence: str
    factors: List[Factor]
    fed: Optional[FedExpectation] = None
    upcoming: List[CalendarItem] = field(default_factory=list)
    releases: List[Release] = field(default_factory=list)
    headlines: List[Headline] = field(default_factory=list)
    deep: Optional[DeepRead] = None
    ai_status: str = ""
    errors: List[str] = field(default_factory=list)
    new_releases: List[Release] = field(default_factory=list)

    @property
    def available_factors(self):
        # type: () -> int
        return sum(1 for factor in self.factors if factor.available)

    @property
    def driver(self):
        # type: () -> str
        """Shortest useful reason for the compact badge."""
        if self.fed is not None:
            return self.fed.headline()
        ranked = sorted((f for f in self.factors if f.available), key=lambda f: -abs(f.points))
        return f"{ranked[0].name} {ranked[0].signal:+.2f}" if ranked else "数据不足"


@dataclass
class Snapshot:
    at: float
    fed: Optional[FedExpectation]
    calendar: List[CalendarItem]
    news: List[NewsItem]
    rates: Optional[Tuple[Series, str]]
    usd: Optional[Tuple[Series, str]]
    errors: List[str]
    calendar_ok: bool
    news_ok: bool


# --------------------------------------------------------------------------- #
# Headline relevance and rule-based tone
# --------------------------------------------------------------------------- #

_STRONG_WORDS = ("美联储", "FOMC", "鲍威尔", "沃什", "非农", "美债", "黄金", "金价", "美元指数", "购金")
_US_MACRO_WORDS = ("降息", "加息", "利率", "通胀", "CPI", "PCE", "就业", "失业", "薪资", "GDP", "零售销售", "ISM")
_NON_FED_BANKS = ("澳洲联储", "澳联储", "新西兰联储")

_HAWKISH_TERMS = (
    "不排除加息", "不排除进一步加息", "不排除再次加息", "不排除进一步收紧",
    "加息", "升息", "上调利率", "提高利率", "鹰派", "偏鹰", "政策收紧", "货币收紧", "收紧货币",
    "进一步收紧", "金融条件收紧", "紧缩", "缩表", "通胀顽固", "通胀粘性", "通胀黏性", "通胀压力",
    "通胀上行", "通胀反弹", "通胀升温", "通胀加速", "再通胀", "抗击通胀", "对抗通胀", "更高更久",
    "维持高利率", "不急于降息", "推迟降息", "降息预期降温", "排除降息", "降息无望", "暂停降息",
    "劳动力市场强劲", "就业强劲", "薪资增长强劲", "经济强劲", "收益率上升", "收益率上涨",
    "收益率走高", "收益率飙升", "收益率攀升", "收益率涨", "美元走强", "美元指数上涨", "美元指数涨",
    "美元升值",
)
_DOVISH_TERMS = (
    "不排除降息", "不排除进一步降息",
    "降息", "减息", "下调利率", "鸽派", "偏鸽", "宽松", "暂停加息", "停止加息", "结束加息", "不再加息",
    "排除加息", "无需加息", "不急于加息", "加息预期降温", "加息周期结束", "通胀回落", "通胀降温",
    "通胀放缓", "通胀缓解", "通胀下降", "通胀接近", "接近2%目标", "就业疲软", "劳动力市场疲软",
    "就业放缓", "失业率上升", "经济放缓", "经济衰退", "衰退风险", "衰退担忧", "经济疲软", "需求疲软",
    "停止缩表", "放缓缩表", "结束缩表", "收益率下降", "收益率下跌", "收益率回落", "收益率走低",
    "收益率跌", "美元走弱", "美元指数下跌", "美元指数跌", "美元贬值", "避险需求", "避险买盘",
    "避险情绪", "购金",
)
_NEGATIONS = ("不急于", "不会", "没有", "排除", "无需", "否认", "难以", "推迟", "放弃", "反对", "不", "未", "无")
_FALSE_NEGATIONS = ("未来", "不仅", "不断", "不少", "不同", "无论", "不过", "无疑")
_REVERSALS = ("预期降温", "预期减弱", "预期消退", "预期回落", "预期下降", "概率下降", "概率降低",
              "降温", "减弱", "消退", "落空", "回落")
_LEXICON = sorted(
    [(term, -1) for term in _HAWKISH_TERMS] + [(term, 1) for term in _DOVISH_TERMS],
    key=lambda pair: -len(pair[0]),
)
_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"


def is_relevant(text):
    # type: (str) -> bool
    if "美联储观察" in text or sum(text.count(mark) for mark in _CIRCLED) >= 2:
        return False  # probability bulletins feed the rate factor; schedules carry no view
    if any(word in text for word in _STRONG_WORDS):
        return True
    if "联储" in text and not any(bank in text for bank in _NON_FED_BANKS):
        return True
    return "美国" in text and any(word in text for word in _US_MACRO_WORDS)


def _negated(text, start):
    # type: (str, int) -> bool
    window = text[max(0, start - 4):start]
    for false in _FALSE_NEGATIONS:
        window = window.replace(false, "  ")
    return any(token in window for token in _NEGATIONS)


def classify_tone(text):
    # type: (str) -> int
    """+1 dovish (bullish gold), -1 hawkish, 0 none/mixed. Longest terms win."""
    taken = [False] * len(text)
    total = 0
    for term, sign in _LEXICON:
        start = text.find(term)
        while start != -1:
            end = start + len(term)
            if not any(taken[start:end]):
                for index in range(start, end):
                    taken[index] = True
                value = sign
                if len(term) <= 3 and _negated(text, start):
                    value = -value
                if any(text[end:end + 6].startswith(r) or r in text[end:end + 4] for r in _REVERSALS):
                    value = -value
                total += value
            start = text.find(term, end)
    return (total > 0) - (total < 0)


# --------------------------------------------------------------------------- #
# Factor scoring (pure functions)
# --------------------------------------------------------------------------- #

def _factor(key):
    # type: (str) -> Factor
    for spec_key, name, weight in FACTOR_SPECS:
        if spec_key == key:
            return Factor(spec_key, name, weight)
    raise KeyError(key)


def _fmt_time(ts, with_date=True):
    # type: (float, bool) -> str
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M" if with_date else "%H:%M")


def score_fed(fed, errors=()):
    # type: (Optional[FedExpectation], Sequence[str]) -> Factor
    factor = _factor("fed")
    if fed is None:
        factor.note = "；".join(errors) or "暂无利率预期数据"
        return factor
    expected = fed.expected_bps
    level = -math.tanh(expected / 25.0)
    previous = fed.prev_expected_bps
    change_text = ""
    if previous is not None:
        delta = expected - previous
        factor.signal = 0.5 * level - 0.5 * math.tanh(delta / 8.0)
        change_text = f"（24h {delta:+.1f}bp）"
    else:
        factor.signal = level
    factor.value = f"{fed.headline()} · 预期 {expected:+.1f}bp{change_text}"
    factor.note = f"{fed.source} · {fed.meeting_month}月议息 {_fmt_time(fed.meeting_ts)}"
    return factor


def scored_releases(calendar, now):
    # type: (Sequence[CalendarItem], float) -> List[Release]
    releases = []
    for item in calendar:
        spec = INDICATORS.get(item.event)
        if (spec is None or item.country != "US" or item.actual is None or item.forecast is None
                or item.ts > now or now - item.ts > RELEASE_WINDOW):
            continue
        label, direction, scale, weight, key = spec
        if "终值" in item.title:
            weight *= 0.4  # final revisions rarely move rate expectations
        surprise = max(-3.0, min(3.0, (item.actual - item.forecast) / scale))
        releases.append(Release(item, label, surprise, -direction * surprise, weight, key))
    return sorted(releases, key=lambda release: -release.item.ts)


def score_data(calendar, now, available):
    # type: (Sequence[CalendarItem], float, bool) -> Tuple[Factor, List[Release]]
    factor = _factor("data")
    releases = scored_releases(calendar, now)
    if not available:
        factor.note = "经济日历不可用"
        return factor, releases
    total = 0.0
    for release in releases:
        decay = 0.5 ** ((now - release.item.ts) / 3600.0 / RELEASE_HALF_LIFE_HOURS)
        total += release.weight * decay * release.impact
    factor.signal = math.tanh(total / 2.0)
    notable = sorted(releases, key=lambda r: -abs(r.weight * r.impact))[:2]
    factor.value = "；".join(f"{r.describe()} {r.verdict}" for r in notable) or "近 10 日无关键数据"
    factor.note = f"近 10 日 {len(releases)} 项美国数据 · 3 日半衰"
    return factor, releases


def score_series(key, series_and_source, lookback=5):
    # type: (str, Optional[Tuple[Series, str]], int) -> Factor
    factor = _factor(key)
    if series_and_source is None:
        factor.note = "数据源不可用"
        return factor
    series, source = series_and_source
    change = series.change(lookback)
    if change is None:
        factor.note = f"{source} 数据不足"
        return factor
    latest, delta, as_of = change
    if key == "rates":
        bps = delta * 100
        factor.signal = -math.tanh(bps / 15.0)
        name = "10Y 实际利率" if series.id == "DFII10" else "10Y 美债收益率"
        factor.value = f"{name} {latest:.2f}%（5日 {bps:+.0f}bp）"
    else:
        base = latest - delta
        pct = delta / base * 100 if base else 0.0
        factor.signal = -math.tanh(pct / 1.0)
        factor.value = f"美元指数 {latest:.2f}（5日 {pct:+.2f}%）"
    factor.note = f"{source} · 截至 {as_of[5:]}"
    return factor


def tag_headlines(news, now, ai_tags):
    # type: (Sequence[NewsItem], float, Dict[str, int]) -> List[Headline]
    headlines = []
    for item in news:
        if now - item.ts > NEWS_WINDOW * 1.5 or item.ts > now + 300 or not is_relevant(item.text):
            continue
        if item.id in ai_tags:
            headlines.append(Headline(item, ai_tags[item.id], "AI"))
        else:
            headlines.append(Headline(item, classify_tone(item.text), "词典"))
    return sorted(headlines, key=lambda headline: -headline.item.ts)


def score_news(headlines, now, available):
    # type: (Sequence[Headline], float, bool) -> Factor
    factor = _factor("news")
    if not available:
        factor.note = "快讯源不可用"
        return factor
    weighted, magnitude = 0.0, 0.0
    hawks = doves = 0
    for headline in headlines:
        age = now - headline.item.ts
        if age > NEWS_WINDOW or headline.tone == 0:
            continue
        weight = (1.0 + 0.5 * (min(headline.item.importance, 3) - 1)) * 0.5 ** (age / 3600.0 / NEWS_HALF_LIFE_HOURS)
        weighted += weight * headline.tone
        magnitude += weight
        hawks += headline.tone < 0
        doves += headline.tone > 0
    factor.signal = weighted / (magnitude + 1.5)
    by_ai = sum(1 for h in headlines if h.by == "AI")
    factor.value = f"24h 偏鹰 {hawks} 条 · 偏鸽 {doves} 条"
    factor.note = f"{'DeepSeek' if by_ai else '规则词典'}判读 · 8 小时半衰"
    return factor


def stance_for(score):
    # type: (Optional[int]) -> Tuple[str, int]
    if score is None:
        return "数据不足", 0
    if score >= 40:
        return "明显偏多", 1
    if score >= 15:
        return "偏多", 1
    if score <= -40:
        return "明显偏空", -1
    if score <= -15:
        return "偏空", -1
    return "中性", 0


def combine(factors):
    # type: (Sequence[Factor]) -> Tuple[Optional[int], str, int, str]
    coverage = sum(f.weight for f in factors if f.available)
    if coverage < 0.25:
        return None, "数据不足", 0, "低"
    total = sum(f.weight * f.signal for f in factors if f.available)
    spread = sum(f.weight * abs(f.signal) for f in factors if f.available)
    agreement = abs(total) / spread if spread > 1e-9 else 1.0
    score = int(round(max(-100.0, min(100.0, total * 100))))
    stance, tone = stance_for(score)
    quality = coverage * (0.5 + 0.5 * agreement)
    confidence = "高" if quality >= 0.7 else "中" if quality >= 0.45 else "低"
    return score, stance, tone, confidence


def is_watch_event(item):
    # type: (CalendarItem) -> bool
    spec = INDICATORS.get(item.event)
    return item.kind == "data" and item.country == "US" and (bool(spec and spec[4]) or item.importance >= 4)


def upcoming_events(calendar, now, limit=5):
    # type: (Sequence[CalendarItem], float, int) -> List[CalendarItem]
    """Pending US releases and Fed events, always including the next FOMC decision."""
    chosen = {}  # type: Dict[Tuple[str, int], CalendarItem]
    for item in calendar:
        if item.country != "US" or item.actual is not None or item.ts < now - 15 * 60:
            continue
        if item.kind == "data" and not (item.event in INDICATORS or item.importance >= 3):
            continue
        if item.kind == "event" and not (item.importance >= 3 and is_relevant(item.title)):
            continue
        chosen.setdefault((item.event, int(item.ts)), item)
    ordered = sorted(chosen.values(), key=lambda item: item.ts)
    fomc_ts = src.next_fomc(now)
    fomc = [] if fomc_ts is None else [CalendarItem(
        id=f"fomc:{int(fomc_ts)}", ts=fomc_ts, title="FOMC 利率决议", event="FOMC利率决议",
        country="US", importance=4, kind="event", source="美联储",
    )]
    ordered = [item for item in ordered if "利率决议" not in item.event]
    return sorted(ordered[: limit - len(fomc)] + fomc, key=lambda item: item.ts)


def next_refresh_delay(outlook, now, base=300.0):
    # type: (Optional[Outlook], float, float) -> float
    """Poll quickly around key US releases so their numbers land within a minute."""
    delay = base
    for item in outlook.upcoming if outlook else ():
        if not is_watch_event(item):
            continue
        if item.ts <= now:
            if now - item.ts <= 15 * 60:
                delay = min(delay, 30.0)
        else:
            delay = min(delay, max(15.0, item.ts - now + 20.0))
    return delay


def build_outlook(snapshot, now, ai_tags=None, deep=None, ai_status=""):
    # type: (Snapshot, float, Optional[Dict[str, int]], Optional[DeepRead], str) -> Outlook
    fed_factor = score_fed(snapshot.fed, [e for e in snapshot.errors if e.startswith(("Kalshi", "Polymarket", "CME"))])
    data_factor, releases = score_data(snapshot.calendar, now, snapshot.calendar_ok)
    headlines = tag_headlines(snapshot.news, now, ai_tags or {})
    factors = [
        fed_factor,
        data_factor,
        score_series("rates", snapshot.rates),
        score_series("usd", snapshot.usd),
        score_news(headlines, now, snapshot.news_ok),
    ]
    score, stance, tone, confidence = combine(factors)
    return Outlook(
        generated_at=now, score=score, stance=stance, tone=tone, confidence=confidence,
        factors=factors, fed=snapshot.fed, upcoming=upcoming_events(snapshot.calendar, now),
        releases=releases, headlines=headlines, deep=deep, ai_status=ai_status,
        errors=list(snapshot.errors),
    )


def build_brief(outlook, trigger, price_context=""):
    # type: (Outlook, str, str) -> str
    """Compact context for the deep read: about 1–2K tokens at most."""
    offset = datetime.now().astimezone().strftime("%z")
    lines = [f"触发：{trigger}", f"时间：{_fmt_time(outlook.generated_at)}（UTC{offset[:3]}:{offset[3:]}）"]
    if price_context:
        lines.append(f"金价：{price_context}")
    lines.append(f"规则评分：{outlook.stance} {outlook.score if outlook.score is not None else '--'}"
                 f"（-100~100，置信度{outlook.confidence}）")
    lines.append("因子（信号 -1~1，正数利多黄金）：")
    for factor in outlook.factors:
        signal = f"{factor.signal:+.2f}" if factor.available else "不可用"
        lines.append(f"- {factor.name}（权重{factor.weight:.2f}）：{factor.value or factor.note} → {signal}")
    if outlook.releases:
        lines.append("近期美国数据：" + "；".join(
            f"{_fmt_time(r.item.ts)} {r.describe()} {r.verdict}" for r in outlook.releases[:6]))
    if outlook.upcoming:
        lines.append("即将公布：" + "；".join(
            f"{_fmt_time(item.ts)} {item.event}" + (f"（预期{item.forecast_text}）" if item.forecast_text else "")
            for item in outlook.upcoming))
    marks = {1: "鸽", -1: "鹰", 0: "中"}
    heads = [h for h in outlook.headlines if h.tone != 0][:6] or outlook.headlines[:4]
    if heads:
        lines.append("要闻：")
        lines.extend(f"- [{marks[h.tone]}] {_fmt_time(h.item.ts, False)} {clip(h.item.text, 90)}" for h in heads)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Service: fetch → score, and plan the (slower) AI work separately
# --------------------------------------------------------------------------- #

@dataclass
class AiJob:
    headlines: List[NewsItem]
    context: str
    deep_trigger: str = ""
    manual: bool = False
    releases: List[str] = field(default_factory=list)  # key prints this deep read covers


class MacroService:
    """Owns cached inputs. ``refresh`` and ``run_ai`` run on worker threads."""

    def __init__(self, analyst=None, http_get=None, clock=time.time):
        # type: (Optional[NewsAnalyst], Optional[Callable], Callable[[], float]) -> None
        self.analyst = analyst
        self._http = http_get or src.default_http_get
        self._clock = clock
        self._lock = threading.RLock()
        self._series = {}  # type: Dict[str, Tuple[float, Series, str]]
        self._forexfactory = None  # type: Optional[Tuple[float, List[CalendarItem]]]
        self._snapshot = None  # type: Optional[Snapshot]
        self._cooldown = {}  # type: Dict[str, Tuple[float, str]]
        self._seen_releases = set()  # type: Set[str]
        # Key prints no deep read has covered yet: id -> (first seen, label). They
        # survive a cooldown or a busy analyst until a read actually runs.
        self._unread_releases = {}  # type: Dict[str, Tuple[float, str]]
        self._primed = False
        self.price_context = ""

    # ------------------------------------------------------------- fetching
    def refresh(self):
        # type: () -> Outlook
        now = self._clock()
        get = self._http
        fed_attempts = (
            ("Kalshi", lambda: src.fetch_kalshi(get, now)),
            ("Polymarket", lambda: src.fetch_polymarket(get, now)),
            ("CME观察", lambda: src.fetch_fedwatch_bulletin(get, now)),
        )
        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="macro") as pool:
            fed_job = pool.submit(self._try_sources, fed_attempts, now)
            calendar_job = pool.submit(src.safe_call, src.fetch_wscn_calendar, get, now - RELEASE_WINDOW, now + 8 * 86400)
            news_job = pool.submit(src.safe_call, src.fetch_wscn_news, get)
            rates_job = pool.submit(self._series_input, "rates", now, (
                ("FRED DFII10", lambda: src.fetch_fred(get, "DFII10", self._fred_start(now))),
                ("FRED DGS10", lambda: src.fetch_fred(get, "DGS10", self._fred_start(now))),
            ))
            usd_job = pool.submit(self._series_input, "usd", now, (
                ("新浪 ICE美元指数", lambda: src.fetch_sina_dxy(get)),
            ))
            fed, errors = fed_job.result()
            calendar, calendar_error = calendar_job.result()
            news, news_error = news_job.result()
            rates, rates_errors = rates_job.result()
            usd, usd_errors = usd_job.result()

        errors = list(errors) + rates_errors + usd_errors
        calendar_ok = calendar is not None
        if calendar_error:
            errors.append(f"华尔街见闻日历: {calendar_error}")
            calendar = self._forexfactory_calendar(now, errors)
        if news_error:
            errors.append(f"华尔街见闻快讯: {news_error}")
        snapshot = Snapshot(
            at=now, fed=fed, calendar=calendar or [], news=news or [], rates=rates, usd=usd,
            errors=errors, calendar_ok=calendar_ok, news_ok=news is not None,
        )
        with self._lock:
            self._snapshot = snapshot
            outlook = self._build_locked(now)
            outlook.new_releases = self._detect_new_releases(outlook.releases, now)
            for release in outlook.new_releases:
                self._unread_releases[release.item.id] = (now, release.label)
            self._unread_releases = {key: value for key, value in self._unread_releases.items()
                                     if now - value[0] <= UNREAD_RELEASE_WINDOW}
        return outlook

    @staticmethod
    def _fred_start(now):
        # type: (float) -> str
        return (datetime.fromtimestamp(now) - timedelta(days=45)).strftime("%Y-%m-%d")

    def _try_sources(self, attempts, now):
        # type: (Sequence[Tuple[str, Callable]], float) -> Tuple[object, List[str]]
        """First source that answers wins. A failed source sits out for a while, so a
        blocked host (common for overseas APIs) does not add a timeout to every refresh."""
        with self._lock:
            resting = {name: self._cooldown[name][1] for name, _ in attempts
                       if self._cooldown.get(name, (0.0, ""))[0] > now}
        ready = [(name, call) for name, call in attempts if name not in resting]
        errors = [f"{name}: {reason}（{SOURCE_COOLDOWN // 60} 分钟内不再重试）" for name, reason in resting.items()]
        if not ready:  # nothing else to fall back on: try them all again
            ready, errors = list(attempts), []
        for name, call in ready:
            result, error = src.safe_call(call)
            with self._lock:
                if result is not None:
                    self._cooldown.pop(name, None)
                else:
                    self._cooldown[name] = (now + SOURCE_COOLDOWN, error or "无数据")
            if result is not None:
                return result, errors
            errors.append(f"{name}: {error or '无数据'}")
        return None, errors

    def _series_input(self, key, now, attempts):
        # type: (str, float, Sequence) -> Tuple[Optional[Tuple[Series, str]], List[str]]
        with self._lock:
            cached = self._series.get(key)
        if cached and now - cached[0] < SERIES_TTL[key]:
            return (cached[1], cached[2]), []
        labelled = [(name, lambda call=call, name=name: (call(), name)) for name, call in attempts]
        result, errors = self._try_sources(labelled, now)
        if result is not None:
            series, name = result
            with self._lock:
                self._series[key] = (now, series, name)
            return (series, name), errors
        if cached and now - cached[0] < SERIES_MAX_AGE:
            return (cached[1], cached[2] + "（缓存）"), errors
        return None, errors

    def _forexfactory_calendar(self, now, errors):
        # type: (float, List[str]) -> List[CalendarItem]
        with self._lock:
            cached = self._forexfactory
        if cached and now - cached[0] < FOREXFACTORY_TTL:
            return cached[1]
        items, error = src.safe_call(src.fetch_forexfactory, self._http)
        if items is None:
            errors.append(f"Forex Factory: {error}")
            return cached[1] if cached else []
        with self._lock:
            self._forexfactory = (now, items)
        return items

    def _detect_new_releases(self, releases, now):
        # type: (Sequence[Release], float) -> List[Release]
        """Key prints that appeared since the last refresh (recent ones only)."""
        horizon = 6 * 3600 if self._primed else 30 * 60
        fresh = [r for r in releases if r.key and r.item.id not in self._seen_releases and now - r.item.ts <= horizon]
        self._seen_releases.update(r.item.id for r in releases)
        self._primed = True
        return fresh

    # -------------------------------------------------------------- scoring
    def _build_locked(self, now):
        # type: (float) -> Outlook
        analyst = self.analyst
        tags = analyst.cached_tags() if analyst and analyst.active else {}
        return build_outlook(
            self._snapshot, now, ai_tags=tags,
            deep=analyst.last_deep if analyst else None,
            ai_status=analyst.status_text() if analyst else "",
        )

    def rebuild(self):
        # type: () -> Optional[Outlook]
        """Re-score the latest inputs (e.g. after new AI tags) without the network."""
        with self._lock:
            if self._snapshot is None:
                return None
            return self._build_locked(self._clock())

    # ------------------------------------------------------------------- AI
    def plan_ai(self, outlook, manual=False, market_open=True):
        # type: (Optional[Outlook], bool, bool) -> Optional[AiJob]
        """``market_open=False`` (every gold market shut) skips the scheduled read."""
        analyst = self.analyst
        if analyst is None or not analyst.active or outlook is None:
            return None
        recent = [h.item for h in outlook.headlines if outlook.generated_at - h.item.ts <= NEWS_WINDOW]
        pending = analyst.pending(recent) if analyst.tag_ready() else []
        with self._lock:
            releases = dict(self._unread_releases)
        trigger = ""
        if analyst.deep_ready(manual, release=bool(releases)):
            labels = [label for _, label in sorted(releases.values())]
            trigger = self._deep_trigger(outlook, manual, market_open, labels)
        if not pending and not trigger:
            return None
        context = f"市场定价 {outlook.fed.headline()}（预期 {outlook.fed.expected_bps:+.0f}bp）" if outlook.fed else ""
        return AiJob(pending, context, trigger, manual, list(releases) if trigger else [])

    def _deep_trigger(self, outlook, manual, market_open=True, releases=()):
        # type: (Outlook, bool, bool, Sequence[str]) -> str
        if manual:
            return "手动请求"
        if outlook.score is None:
            return ""
        if releases:
            return "数据公布：" + "、".join(releases[:3])
        last = self.analyst.last_deep if self.analyst else None
        if last is None:
            return "定时解读" if market_open else ""
        if market_open and outlook.generated_at - last.at >= DEEP_SCHEDULE:
            return "定时解读"
        if (outlook.fed and last.expected_bps is not None
                and abs(outlook.fed.expected_bps - last.expected_bps) >= DEEP_RATE_SHIFT_BPS):
            return f"利率预期变化 {outlook.fed.expected_bps - last.expected_bps:+.0f}bp"
        if last.score is not None:
            delta = outlook.score - last.score
            if stance_for(last.score)[1] != outlook.tone and abs(delta) >= DEEP_FLIP_MARGIN:
                return "评分方向变化"
            if abs(delta) >= DEEP_SCORE_SHIFT:
                return f"评分变化 {delta:+d}"
        return ""

    def run_ai(self, job):
        # type: (AiJob) -> Optional[Outlook]
        analyst = self.analyst
        if analyst is None:
            return self.rebuild()
        if job.headlines:
            analyst.tag_headlines(job.headlines, job.context)
        if job.deep_trigger:
            interim = self.rebuild()
            if interim is not None:
                analyst.deep_read(
                    build_brief(interim, job.deep_trigger, self.price_context), job.deep_trigger,
                    manual=job.manual, expected_bps=interim.fed.expected_bps if interim.fed else None,
                    score=interim.score, release=bool(job.releases),
                )
                with self._lock:
                    for key in job.releases:
                        self._unread_releases.pop(key, None)
        return self.rebuild()

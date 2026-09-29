import math
import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import requests

HEADERS = {"User-Agent": "Mozilla/5.0"}
REQUEST_TIMEOUT = (4, 8)

# One pooled session: quotes refresh every few seconds, so keep-alive saves a
# TLS handshake per request. Fetches run on one worker thread at a time.
_session = requests.Session()
_session.headers.update(HEADERS)

# 招行金交所接口
CMB_URL = "https://m.cmbchina.com/api/rate/gold"

# Swissquote 国际金价 + 离岸人民币汇率
SQ_GOLD_URL = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD"
SQ_CNH_URL = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/USD/CNH"

TROY_OZ_TO_GRAM = 31.1035
SOURCE_FALLBACK_COOLDOWN_SECONDS = 120
CMB_QUOTE_MAX_AGE_SECONDS = 180
SWISSQUOTE_QUOTE_MAX_AGE_SECONDS = 180
QUOTE_FUTURE_TOLERANCE_SECONDS = 10

_source_unhealthy_until = {
    "cmb": 0.0,
    "intl": 0.0,
}
_last_success_source = None  # type: Optional[str]

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_ZURICH_TZ = ZoneInfo("Europe/Zurich")

# 结束时间保留交易所公布的最后一秒，然后立即切换。
_CMB_DAY_SESSIONS = (
    ((9, 0, 0), (11, 30, 1)),
    ((13, 30, 0), (15, 30, 1)),
)
_CMB_NIGHT_START = (19, 50, 0)
_CMB_NIGHT_END = (2, 30, 1)

# Swissquote 2026 产品表中的 XAU/USD 交易时间（Europe/Zurich）。
_INTL_SESSION_START = (0, 5, 0)
_INTL_SESSION_END = (22, 55, 1)


def _market_now(now, tz):
    # type: (Optional[datetime], ZoneInfo) -> datetime
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _is_cmb_trading_time(now=None):
    # type: (Optional[datetime]) -> bool
    current = _market_now(now, _SHANGHAI_TZ)
    weekday = current.weekday()
    current_time = (current.hour, current.minute, current.second)

    if weekday < 5:
        for start, end in _CMB_DAY_SESSIONS:
            if start <= current_time < end:
                return True
        if current_time >= _CMB_NIGHT_START:
            return True

    # 周一至周五的夜盘会延续到次日，因此周二至周六凌晨有效。
    return 1 <= weekday <= 5 and current_time < _CMB_NIGHT_END


def _is_intl_trading_time(now=None):
    # type: (Optional[datetime]) -> bool
    current = _market_now(now, _ZURICH_TZ)
    current_time = (current.hour, current.minute, current.second)
    return (
        current.weekday() < 5
        and _INTL_SESSION_START <= current_time < _INTL_SESSION_END
    )


def _scheduled_sources(now=None):
    # type: (Optional[datetime]) -> list[str]
    sources = []
    if _is_cmb_trading_time(now):
        sources.append("cmb")
    if _is_intl_trading_time(now):
        sources.append("intl")
    return sources


def is_market_open(now=None):
    # type: (Optional[datetime]) -> bool
    """任一渠道处于交易时段；周末和每日休市时两边都关闭。"""
    return bool(_scheduled_sources(now))


def _combine_market_time(day, time_parts, tz):
    # type: (date, tuple[int, int, int], ZoneInfo) -> datetime
    return datetime.combine(
        day,
        clock_time(*time_parts),
        tzinfo=tz,
    )


def seconds_until_next_market_transition(now=None):
    # type: (Optional[datetime]) -> float
    """返回下一次任一渠道计划开/收市前的秒数。"""
    base = _market_now(now, _SHANGHAI_TZ)
    base_utc = base.astimezone(timezone.utc)
    candidates = []

    shanghai_day = base.astimezone(_SHANGHAI_TZ).date()
    zurich_day = base.astimezone(_ZURICH_TZ).date()
    for offset in range(9):
        cmb_day = shanghai_day + timedelta(days=offset)
        if cmb_day.weekday() < 5:
            for boundary in (
                _CMB_DAY_SESSIONS[0][0],
                _CMB_DAY_SESSIONS[0][1],
                _CMB_DAY_SESSIONS[1][0],
                _CMB_DAY_SESSIONS[1][1],
                _CMB_NIGHT_START,
            ):
                candidates.append(_combine_market_time(cmb_day, boundary, _SHANGHAI_TZ))
            candidates.append(
                _combine_market_time(cmb_day + timedelta(days=1), _CMB_NIGHT_END, _SHANGHAI_TZ)
            )

        intl_day = zurich_day + timedelta(days=offset)
        if intl_day.weekday() < 5:
            candidates.append(_combine_market_time(intl_day, _INTL_SESSION_START, _ZURICH_TZ))
            candidates.append(_combine_market_time(intl_day, _INTL_SESSION_END, _ZURICH_TZ))

    future_delays = [
        (candidate.astimezone(timezone.utc) - base_utc).total_seconds()
        for candidate in candidates
        if candidate.astimezone(timezone.utc) > base_utc
    ]
    return max(1.0, min(future_delays)) if future_delays else 3600.0


def _sq_quote(url):
    # type: (str) -> Optional[Dict[str, float]]
    resp = _session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or not data or not isinstance(data[0], dict):
        return None
    profiles = data[0].get("spreadProfilePrices")
    if not isinstance(profiles, list) or not profiles or not isinstance(profiles[0], dict):
        return None
    bid = _finite_number(profiles[0].get("bid"), positive=True)
    ask = _finite_number(profiles[0].get("ask"), positive=True)
    timestamp = _epoch_timestamp(data[0].get("ts"))
    if bid is None or ask is None or timestamp is None or bid > ask:
        return None
    return {
        "price": bid + (ask - bid) / 2,
        "timestamp": timestamp,
    }


def _finite_number(raw_value, positive=False):
    # type: (Any, bool) -> Optional[float]
    """外部数字必须有限；布尔值不能作为报价使用。"""
    if isinstance(raw_value, bool):
        return None
    try:
        value = float(raw_value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(value) or (positive and value <= 0):
        return None
    return value


def _epoch_timestamp(raw_timestamp):
    # type: (Any) -> Optional[float]
    timestamp = _finite_number(raw_timestamp, positive=True)
    if timestamp is not None and timestamp > 10_000_000_000:
        timestamp /= 1000.0
    return timestamp


def _cmb_quote_age_seconds(raw_time, now=None):
    # type: (Any, Optional[datetime]) -> Optional[float]
    if not raw_time:
        return None

    current = _market_now(now, _SHANGHAI_TZ)
    parsed_time = None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            parsed_time = datetime.strptime(str(raw_time), fmt).time()
            break
        except ValueError:
            continue
    if parsed_time is None:
        return None

    quote_time = datetime.combine(current.date(), parsed_time, tzinfo=_SHANGHAI_TZ)
    if quote_time > current + timedelta(seconds=QUOTE_FUTURE_TOLERANCE_SECONDS):
        quote_time -= timedelta(days=1)
    return (current - quote_time).total_seconds()


def _is_cmb_quote_fresh(raw_time, now=None):
    # type: (Any, Optional[datetime]) -> bool
    age = _cmb_quote_age_seconds(raw_time, now)
    return age is not None and -QUOTE_FUTURE_TOLERANCE_SECONDS <= age <= CMB_QUOTE_MAX_AGE_SECONDS


def _is_epoch_quote_fresh(raw_timestamp, now=None):
    # type: (Any, Optional[datetime]) -> bool
    quote_timestamp = _epoch_timestamp(raw_timestamp)
    if quote_timestamp is None:
        return False
    current_timestamp = _market_now(now, _ZURICH_TZ).timestamp()
    age = current_timestamp - quote_timestamp
    return -QUOTE_FUTURE_TOLERANCE_SECONDS <= age <= SWISSQUOTE_QUOTE_MAX_AGE_SECONDS


def _fetch_cmb(now=None):
    # type: (Optional[datetime]) -> Dict[str, Any]
    """从招行获取 Au(T+D) 价格，休市时返回 ok=False"""
    try:
        resp = _session.get(CMB_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return {"ok": False, "error": f"cmb request failed: {exc}"}

    if not isinstance(data, dict):
        return {"ok": False, "error": "cmb payload is not an object"}
    if data.get("returnCode") != "SUC0000":
        return {"ok": False, "error": f"cmb returned {data.get('returnCode')}"}

    body = data.get("body")
    items = body.get("data") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return {"ok": False, "error": "cmb payload has no quote list"}
    for item in items:
        if not isinstance(item, dict) or item.get("goldNo") != "AUTD":
            continue

        current = _market_now(now, _SHANGHAI_TZ)
        if not _is_cmb_trading_time(current):
            return {"ok": False, "error": "cmb market is outside trading hours"}
        age = _cmb_quote_age_seconds(item.get("time"), current)
        if age is None or not -QUOTE_FUTURE_TOLERANCE_SECONDS <= age <= CMB_QUOTE_MAX_AGE_SECONDS:
            return {
                "ok": False,
                "error": f"cmb quote is stale (time={item.get('time', '')})",
            }
        price = _finite_number(item.get("curPrice"), positive=True)
        if price is None:
            return {"ok": False, "error": "cmb current price is invalid"}
        pre_close = _finite_number(item.get("preClose"), positive=True)
        change = _finite_number(item.get("upDown"))
        change_pct = None
        if pre_close is not None and change is not None:
            change_pct = _finite_number(change / pre_close * 100)
        if change_pct is None:
            change = None
        else:
            change_pct = round(change_pct, 2)
        return {
            "ok": True,
            "data": {
                "price": price,
                "change": change,
                "change_pct": change_pct,
                "high": _finite_number(item.get("high"), positive=True) or 0.0,
                "low": _finite_number(item.get("low"), positive=True) or 0.0,
                "time": item["time"],
                "quote_timestamp": current.timestamp() - age,
                "source": "cmb",
            },
        }

    return {"ok": False, "error": "cmb returned no AUTD quote"}


def _fetch_swissquote(now=None):
    # type: (Optional[datetime]) -> Dict[str, Any]
    """从 Swissquote 获取国际金价并换算人民币/克"""
    try:
        if not _is_intl_trading_time(now):
            return {"ok": False, "error": "swissquote market is outside trading hours"}

        gold_quote = _sq_quote(SQ_GOLD_URL)
        if gold_quote is None:
            return {"ok": False, "error": "swissquote XAU/USD returned empty or invalid data"}
        cnh_quote = _sq_quote(SQ_CNH_URL)
        if cnh_quote is None:
            return {"ok": False, "error": "swissquote USD/CNH returned empty or invalid data"}
        # 两次请求都结束后用同一时刻校验，避免跨过收盘或报价有效期。
        current = _market_now(now, _ZURICH_TZ)
        if not _is_intl_trading_time(current):
            return {"ok": False, "error": "swissquote market is outside trading hours"}
        if not _is_epoch_quote_fresh(gold_quote.get("timestamp"), current):
            return {"ok": False, "error": "swissquote XAU/USD quote is stale"}
        if not _is_epoch_quote_fresh(cnh_quote.get("timestamp"), current):
            return {"ok": False, "error": "swissquote USD/CNH quote is stale"}

        rmb_gram = _finite_number(
            round(gold_quote["price"] * cnh_quote["price"] / TROY_OZ_TO_GRAM, 2),
            positive=True,
        )
        if rmb_gram is None:
            return {"ok": False, "error": "swissquote converted price is invalid"}

        return {
            "ok": True,
            "data": {
                "price": rmb_gram,
                # 国际现货与 Au(T+D) 不是同一标的，不能借用其昨收计算日涨跌。
                "change": None,
                "change_pct": None,
                "high": 0.0,
                "low": 0.0,
                "time": "",
                "quote_timestamp": min(gold_quote["timestamp"], cnh_quote["timestamp"]),
                "source": "intl",
            },
        }
    except Exception as exc:
        return {"ok": False, "error": f"swissquote failed: {exc}"}


def _fetch_source(source, now=None):
    # type: (str, Optional[datetime]) -> Dict[str, Any]
    if source == "cmb":
        return _fetch_cmb(now)
    if source == "intl":
        return _fetch_swissquote(now)
    return {"ok": False, "error": f"unknown source: {source}"}


def _is_source_healthy(source):
    # type: (str) -> bool
    return time.monotonic() >= _source_unhealthy_until.get(source, 0.0)


def _ordered_sources(sources):
    # type: (list[str]) -> list[str]
    if not sources:
        return []

    # 计划首选源必须每次先探测，否则它在开市前失败一次后，会被成功的
    # 备用源持续压住，直到健康冷却结束才能切回。
    preferred = sources[0]
    fallbacks = sources[1:]
    healthy = [source for source in fallbacks if _is_source_healthy(source)]
    unhealthy = [source for source in fallbacks if source not in healthy]
    if _last_success_source in healthy:
        healthy.remove(_last_success_source)
        healthy.insert(0, _last_success_source)
    return [preferred] + healthy + unhealthy


def _fetch_with_fallback(sources, now=None):
    # type: (list[str], Optional[datetime]) -> Dict[str, Any]
    global _last_success_source

    errors = []
    preferred_source = sources[0] if sources else None
    for source in _ordered_sources(sources):
        result = _fetch_source(source, now)
        if result.get("ok"):
            _source_unhealthy_until[source] = 0.0
            _last_success_source = source
            data = result.get("data", {})
            if preferred_source and data.get("source") != preferred_source:
                data["fallback_from"] = preferred_source
            return result
        _source_unhealthy_until[source] = time.monotonic() + SOURCE_FALLBACK_COOLDOWN_SECONDS
        errors.append(f"{source}: {result.get('error', 'unknown error')}")
    return {"ok": False, "error": "; ".join(errors)}


def fetch_gold_price_result(now=None):
    # type: (Optional[datetime]) -> Dict[str, Any]
    """按市场有效时间自动选源，并在首选报价失效时回退。"""
    current = _market_now(now, _SHANGHAI_TZ)
    sources = _scheduled_sources(current)
    if not sources:
        return {"ok": False, "status": "closed", "error": "no live gold market is currently scheduled"}

    # 生产请求在网络响应后重新取当前时间，防止请求跨过收盘边界时接纳旧源；
    # 测试传入固定 now 时仍保持完全确定。
    validation_now = current if now is not None else None
    return _fetch_with_fallback(sources, validation_now)

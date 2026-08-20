import time
from datetime import date, datetime, time as clock_time, timedelta, timezone
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import requests

HEADERS = {"User-Agent": "Mozilla/5.0"}

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

# 缓存金交所昨收盘价，供休市时计算日涨跌
_cached_pre_close = None  # type: Optional[float]
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


def _is_trading_time(now=None):
    # type: (Optional[datetime]) -> bool
    """兼容旧调用：交易时段特指招行 Au(T+D) 渠道。"""
    return _is_cmb_trading_time(now)


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
    resp = requests.get(url, timeout=10, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list) and data:
        profiles = data[0].get("spreadProfilePrices", [])
        if profiles:
            return {
                "price": (profiles[0]["bid"] + profiles[0]["ask"]) / 2,
                "timestamp": float(data[0]["ts"]),
            }
    return None


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
    try:
        quote_timestamp = float(raw_timestamp)
    except (TypeError, ValueError):
        return False
    if quote_timestamp > 10_000_000_000:
        quote_timestamp /= 1000.0
    current_timestamp = _market_now(now, _ZURICH_TZ).timestamp()
    age = current_timestamp - quote_timestamp
    return -QUOTE_FUTURE_TOLERANCE_SECONDS <= age <= SWISSQUOTE_QUOTE_MAX_AGE_SECONDS


def _fetch_cmb(now=None):
    # type: (Optional[datetime]) -> Dict[str, Any]
    """从招行获取 Au(T+D) 价格，休市时返回 ok=False"""
    try:
        resp = requests.get(CMB_URL, timeout=10, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        return {"ok": False, "error": f"cmb request failed: {exc}"}

    if data.get("returnCode") != "SUC0000":
        return {"ok": False, "error": f"cmb returned {data.get('returnCode')}"}

    for item in data.get("body", {}).get("data", []):
        if item.get("goldNo") != "AUTD":
            continue
        # 无论是否休市，都缓存昨收盘价
        try:
            global _cached_pre_close
            pre_close = float(item["preClose"])
            if pre_close > 0:
                _cached_pre_close = pre_close
        except (KeyError, TypeError, ValueError):
            pass

        if not _is_cmb_trading_time(now):
            return {"ok": False, "error": "cmb market is outside trading hours"}
        if not _is_cmb_quote_fresh(item.get("time"), now):
            return {
                "ok": False,
                "error": f"cmb quote is stale (time={item.get('time', '')})",
            }
        try:
            price = float(item["curPrice"])
            if price <= 0:
                continue
            pre_close = float(item["preClose"])
            change = float(item["upDown"])
            change_pct = (change / pre_close * 100) if pre_close > 0 else 0
            return {
                "ok": True,
                "data": {
                    "price": price,
                    "change": change,
                    "change_pct": round(change_pct, 2),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "time": item["time"],
                    "source": "cmb",
                },
            }
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "error": f"cmb payload error: {exc}"}

    return {"ok": False, "error": "AUTD price is 0 (market closed)"}


def _fetch_swissquote(now=None):
    # type: (Optional[datetime]) -> Dict[str, Any]
    """从 Swissquote 获取国际金价并换算人民币/克"""
    try:
        if not _is_intl_trading_time(now):
            return {"ok": False, "error": "swissquote market is outside trading hours"}

        gold_quote = _sq_quote(SQ_GOLD_URL)
        cnh_quote = _sq_quote(SQ_CNH_URL)
        if gold_quote is None or cnh_quote is None:
            return {"ok": False, "error": "swissquote returned empty data"}
        if not _is_epoch_quote_fresh(gold_quote.get("timestamp"), now):
            return {"ok": False, "error": "swissquote XAU/USD quote is stale"}
        if not _is_epoch_quote_fresh(cnh_quote.get("timestamp"), now):
            return {"ok": False, "error": "swissquote USD/CNH quote is stale"}

        rmb_gram = round(gold_quote["price"] * cnh_quote["price"] / TROY_OZ_TO_GRAM, 2)

        # 用缓存的昨收盘价计算日涨跌
        change = 0.0
        change_pct = 0.0
        if _cached_pre_close and _cached_pre_close > 0:
            change = round(rmb_gram - _cached_pre_close, 2)
            change_pct = round(change / _cached_pre_close * 100, 2)

        return {
            "ok": True,
            "data": {
                "price": rmb_gram,
                "change": change,
                "change_pct": change_pct,
                "high": 0.0,
                "low": 0.0,
                "time": "",
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
        if _cached_pre_close is None and _is_source_healthy("cmb"):
            _fetch_cmb(now)  # 仅为触发缓存 preClose
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


def fetch_gold_price_result(force_source="auto", now=None):
    # type: (str, Optional[datetime]) -> Dict[str, Any]
    """按市场有效时间自动选源，并在首选报价失效时回退。"""
    current = _market_now(now, _SHANGHAI_TZ)
    sources = _scheduled_sources(current)
    if force_source in ("cmb", "intl") and force_source in sources:
        sources.remove(force_source)
        sources.insert(0, force_source)

    if not sources:
        return {"ok": False, "error": "no live gold market is currently scheduled"}

    # 生产请求在网络响应后重新取当前时间，防止请求跨过收盘边界时接纳旧源；
    # 测试传入固定 now 时仍保持完全确定。
    validation_now = current if now is not None else None
    return _fetch_with_fallback(sources, validation_now)


def fetch_gold_price():
    # type: () -> Optional[Dict[str, Any]]
    result = fetch_gold_price_result()
    return result.get("data") if result.get("ok") else None

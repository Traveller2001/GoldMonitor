from typing import Any, Dict, Optional

import requests

HEADERS = {"User-Agent": "Mozilla/5.0"}

# 招行金交所接口
CMB_URL = "https://m.cmbchina.com/api/rate/gold"

# Swissquote 国际金价 + 离岸人民币汇率
SQ_GOLD_URL = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/XAU/USD"
SQ_CNH_URL = "https://forex-data-feed.swissquote.com/public-quotes/bboquotes/instrument/USD/CNH"

TROY_OZ_TO_GRAM = 31.1035


def _sq_mid_price(url):
    # type: (str) -> Optional[float]
    resp = requests.get(url, timeout=10, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list) and data:
        profiles = data[0].get("spreadProfilePrices", [])
        if profiles:
            return (profiles[0]["bid"] + profiles[0]["ask"]) / 2
    return None


def _fetch_cmb():
    # type: () -> Dict[str, Any]
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
        if item.get("goldNo") != "AUTD" or item.get("curPrice") == "0":
            continue
        try:
            price = float(item["curPrice"])
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


def _fetch_swissquote():
    # type: () -> Dict[str, Any]
    """从 Swissquote 获取国际金价并换算人民币/克"""
    try:
        usd_oz = _sq_mid_price(SQ_GOLD_URL)
        usd_cnh = _sq_mid_price(SQ_CNH_URL)
        if usd_oz is None or usd_cnh is None:
            return {"ok": False, "error": "swissquote returned empty data"}

        rmb_gram = round(usd_oz * usd_cnh / TROY_OZ_TO_GRAM, 2)
        return {
            "ok": True,
            "data": {
                "price": rmb_gram,
                "change": 0.0,
                "change_pct": 0.0,
                "high": 0.0,
                "low": 0.0,
                "time": "",
                "source": "intl",
            },
        }
    except Exception as exc:
        return {"ok": False, "error": f"swissquote failed: {exc}"}


def fetch_gold_price_result():
    # type: () -> Dict[str, Any]
    """混合数据源：优先招行金交所，休市自动切换国际金价"""
    result = _fetch_cmb()
    if result.get("ok"):
        return result
    # 金交所休市，切换到国际金价
    return _fetch_swissquote()


def fetch_gold_price():
    # type: () -> Optional[Dict[str, Any]]
    result = fetch_gold_price_result()
    return result.get("data") if result.get("ok") else None

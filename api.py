from typing import Any, Dict, Optional

import requests

API_URL = "https://m.cmbchina.com/api/rate/gold"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_gold_price_result() -> Dict[str, Any]:
    """Fetch the latest Au(T+D) price with success/error metadata."""
    try:
        resp = requests.get(API_URL, timeout=10, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as exc:
        return {"ok": False, "error": f"request failed: {exc}"}
    except ValueError as exc:
        return {"ok": False, "error": f"invalid json response: {exc}"}

    if data.get("returnCode") != "SUC0000":
        code = data.get("returnCode", "unknown")
        return {"ok": False, "error": f"api returned {code}"}

    items = data.get("body", {}).get("data", [])
    for item in items:
        if item.get("goldNo") != "AUTD" or item.get("curPrice") == "0":
            continue

        try:
            price = float(item["curPrice"])
            pre_close = float(item["preClose"])
            change = float(item["upDown"])
            change_pct = (change / pre_close * 100) if pre_close > 0 else 0
            price_data = {
                "price": price,
                "change": change,
                "change_pct": round(change_pct, 2),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "time": item["time"],
            }
        except (KeyError, TypeError, ValueError) as exc:
            return {"ok": False, "error": f"invalid AUTD payload: {exc}"}

        return {"ok": True, "data": price_data}

    return {"ok": False, "error": "AUTD price unavailable"}


def fetch_gold_price() -> Optional[Dict[str, Any]]:
    result = fetch_gold_price_result()
    return result.get("data") if result.get("ok") else None

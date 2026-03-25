from typing import Optional, Dict

import requests

# 招商银行黄金行情 API（数据来自上海金交所）
API_URL = "https://m.cmbchina.com/api/rate/gold"
HEADERS = {"User-Agent": "Mozilla/5.0"}


def fetch_gold_price() -> Optional[Dict]:
    """获取 Au(T+D) 实时金价，返回 dict 或 None

    返回示例:
    {
        "price": 1019.37,      # 当前价 (元/克)
        "change": 46.59,       # 涨跌额
        "change_pct": 4.76,    # 涨跌幅 %
        "high": 1023.68,       # 最高价
        "low": 967.50,         # 最低价
        "time": "10:24:13",    # 更新时间
    }
    """
    try:
        resp = requests.get(API_URL, timeout=10, headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()

        if data.get("returnCode") != "SUC0000":
            return None

        for item in data["body"]["data"]:
            if item["goldNo"] == "AUTD" and item["curPrice"] != "0":
                price = float(item["curPrice"])
                pre_close = float(item["preClose"])
                change = float(item["upDown"])
                change_pct = (change / pre_close * 100) if pre_close > 0 else 0
                return {
                    "price": price,
                    "change": change,
                    "change_pct": round(change_pct, 2),
                    "high": float(item["high"]),
                    "low": float(item["low"]),
                    "time": item["time"],
                }
        return None
    except Exception:
        return None

import time
import unittest
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import api


SHANGHAI = ZoneInfo("Asia/Shanghai")
ZURICH = ZoneInfo("Europe/Zurich")


class MarketHoursTest(unittest.TestCase):
    def test_cmb_day_and_night_boundaries(self):
        monday = (2026, 8, 17)
        self.assertFalse(api._is_cmb_trading_time(datetime(*monday, 8, 59, 59, tzinfo=SHANGHAI)))
        self.assertTrue(api._is_cmb_trading_time(datetime(*monday, 9, 0, 0, tzinfo=SHANGHAI)))
        self.assertTrue(api._is_cmb_trading_time(datetime(*monday, 11, 30, 0, tzinfo=SHANGHAI)))
        self.assertFalse(api._is_cmb_trading_time(datetime(*monday, 11, 30, 1, tzinfo=SHANGHAI)))
        self.assertTrue(api._is_cmb_trading_time(datetime(*monday, 13, 30, 0, tzinfo=SHANGHAI)))
        self.assertFalse(api._is_cmb_trading_time(datetime(*monday, 15, 30, 1, tzinfo=SHANGHAI)))
        self.assertTrue(api._is_cmb_trading_time(datetime(*monday, 19, 50, 0, tzinfo=SHANGHAI)))

    def test_cmb_weekend_night_session(self):
        self.assertFalse(api._is_cmb_trading_time(datetime(2026, 8, 17, 0, 30, tzinfo=SHANGHAI)))
        self.assertTrue(api._is_cmb_trading_time(datetime(2026, 8, 18, 0, 30, tzinfo=SHANGHAI)))
        self.assertTrue(api._is_cmb_trading_time(datetime(2026, 8, 22, 2, 30, 0, tzinfo=SHANGHAI)))
        self.assertFalse(api._is_cmb_trading_time(datetime(2026, 8, 22, 2, 30, 1, tzinfo=SHANGHAI)))
        self.assertFalse(api._is_cmb_trading_time(datetime(2026, 8, 22, 20, 0, tzinfo=SHANGHAI)))

    def test_swissquote_boundaries_use_zurich_time(self):
        monday = (2026, 8, 17)
        self.assertFalse(api._is_intl_trading_time(datetime(*monday, 0, 4, 59, tzinfo=ZURICH)))
        self.assertTrue(api._is_intl_trading_time(datetime(*monday, 0, 5, 0, tzinfo=ZURICH)))
        self.assertTrue(api._is_intl_trading_time(datetime(*monday, 22, 55, 0, tzinfo=ZURICH)))
        self.assertFalse(api._is_intl_trading_time(datetime(*monday, 22, 55, 1, tzinfo=ZURICH)))
        self.assertFalse(api._is_intl_trading_time(datetime(2026, 8, 23, 12, 0, tzinfo=ZURICH)))

    def test_source_priority_changes_with_market_hours(self):
        self.assertEqual(
            api._scheduled_sources(datetime(2026, 8, 20, 10, 0, tzinfo=SHANGHAI)),
            ["cmb", "intl"],
        )
        self.assertEqual(
            api._scheduled_sources(datetime(2026, 8, 20, 12, 0, tzinfo=SHANGHAI)),
            ["intl"],
        )
        self.assertEqual(
            api._scheduled_sources(datetime(2026, 8, 22, 3, 0, tzinfo=SHANGHAI)),
            ["intl"],
        )
        self.assertEqual(
            api._scheduled_sources(datetime(2026, 8, 23, 12, 0, tzinfo=SHANGHAI)),
            [],
        )

    def test_transition_delay_targets_exact_close(self):
        now = datetime(2026, 8, 20, 11, 29, 59, tzinfo=SHANGHAI)
        self.assertEqual(api.seconds_until_next_market_transition(now), 2.0)


class QuoteValidityTest(unittest.TestCase):
    def test_cmb_timestamp_handles_midnight(self):
        now = datetime(2026, 8, 18, 0, 0, 30, tzinfo=SHANGHAI)
        self.assertEqual(api._cmb_quote_age_seconds("23:59:30", now), 60.0)
        self.assertTrue(api._is_cmb_quote_fresh("23:59:30", now))
        self.assertFalse(api._is_cmb_quote_fresh("23:40:00", now))

    def test_swissquote_timestamp_must_be_recent(self):
        now = datetime(2026, 8, 17, 12, 0, tzinfo=ZURICH)
        self.assertTrue(api._is_epoch_quote_fresh(now.timestamp() * 1000, now))
        self.assertFalse(api._is_epoch_quote_fresh((now.timestamp() - 181) * 1000, now))

    def test_preferred_source_is_not_suppressed_by_cooldown(self):
        previous_until = dict(api._source_unhealthy_until)
        previous_success = api._last_success_source
        try:
            api._source_unhealthy_until["cmb"] = time.monotonic() + 120
            api._last_success_source = "intl"
            self.assertEqual(api._ordered_sources(["cmb", "intl"]), ["cmb", "intl"])
        finally:
            api._source_unhealthy_until.update(previous_until)
            api._last_success_source = previous_success

    def test_stale_preferred_source_falls_back(self):
        def fake_fetch(source, _now):
            if source == "cmb":
                return {"ok": False, "error": "stale"}
            return {"ok": True, "data": {"source": "intl", "price": 1.0}}

        with patch.object(api, "_fetch_source", side_effect=fake_fetch):
            result = api._fetch_with_fallback(["cmb", "intl"], datetime.now(SHANGHAI))

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["source"], "intl")
        self.assertEqual(result["data"]["fallback_from"], "cmb")

    def test_no_scheduled_market_does_not_request_stale_data(self):
        sunday = datetime(2026, 8, 23, 12, 0, tzinfo=SHANGHAI)
        with patch.object(api, "_fetch_with_fallback") as fetch:
            result = api.fetch_gold_price_result(now=sunday)
        self.assertFalse(result["ok"])
        fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()

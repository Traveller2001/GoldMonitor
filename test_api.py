import time
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
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
    def setUp(self):
        health = patch.dict(api._source_unhealthy_until, {"cmb": 0.0, "intl": 0.0}, clear=True)
        health.start()
        self.addCleanup(health.stop)
        last_source = patch.object(api, "_last_success_source", None)
        last_source.start()
        self.addCleanup(last_source.stop)

    def test_cmb_timestamp_handles_midnight(self):
        now = datetime(2026, 8, 18, 0, 0, 30, tzinfo=SHANGHAI)
        self.assertEqual(api._cmb_quote_age_seconds("23:59:30", now), 60.0)
        self.assertTrue(api._is_cmb_quote_fresh("23:59:30", now))
        self.assertFalse(api._is_cmb_quote_fresh("23:40:00", now))

    def test_swissquote_timestamp_must_be_recent(self):
        now = datetime(2026, 8, 17, 12, 0, tzinfo=ZURICH)
        self.assertTrue(api._is_epoch_quote_fresh(now.timestamp() * 1000, now))
        self.assertFalse(api._is_epoch_quote_fresh((now.timestamp() - 181) * 1000, now))

    def test_quote_timestamp_rejects_invalid_or_future_values(self):
        now = datetime(2026, 8, 17, 12, 0, tzinfo=ZURICH)
        for timestamp in (None, True, {}, "NaN", "Infinity", float("-inf"), now.timestamp() + 11):
            with self.subTest(timestamp=timestamp):
                self.assertFalse(api._is_epoch_quote_fresh(timestamp, now))
        self.assertFalse(api._is_cmb_quote_fresh("12:00:11", now.astimezone(SHANGHAI)))

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
        self.assertEqual(result["status"], "closed")
        fetch.assert_not_called()


class QuotePayloadTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 20, 10, 0, tzinfo=SHANGHAI)

    def cmb_payload(self, **overrides):
        item = {
            "goldNo": "AUTD", "curPrice": "750.50", "preClose": "748.00",
            "upDown": "2.50", "high": "755.00", "low": "745.00", "time": "09:59:30",
        }
        item.update(overrides)
        return {"returnCode": "SUC0000", "body": {"data": [item]}}

    def response(self, payload):
        response = Mock()
        response.json.return_value = payload
        return response

    def sq_payload(self, bid="2400", ask="2402", timestamp=None):
        return [{
            "ts": self.now.timestamp() * 1000 if timestamp is None else timestamp,
            "spreadProfilePrices": [{"bid": bid, "ask": ask}],
        }]

    def test_cmb_valid_quote_has_finite_prices_and_exchange_timestamp(self):
        with patch.object(api.requests, "get", return_value=self.response(self.cmb_payload())):
            result = api._fetch_cmb(self.now)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["price"], 750.5)
        self.assertEqual(result["data"]["change_pct"], 0.33)
        self.assertEqual(result["data"]["quote_timestamp"], self.now.timestamp() - 30)

    def test_cmb_rejects_malformed_envelopes_without_raising(self):
        for payload in (None, [], "invalid", {}, {"returnCode": "SUC0000", "body": None},
                        {"returnCode": "SUC0000", "body": {"data": {}}}):
            with self.subTest(payload=payload):
                with patch.object(api.requests, "get", return_value=self.response(payload)):
                    self.assertFalse(api._fetch_cmb(self.now)["ok"])

    def test_cmb_ignores_unrelated_malformed_rows(self):
        payload = self.cmb_payload()
        payload["body"]["data"].insert(0, None)
        with patch.object(api.requests, "get", return_value=self.response(payload)):
            self.assertTrue(api._fetch_cmb(self.now)["ok"])

    def test_cmb_rejects_invalid_current_prices(self):
        for price in (None, True, {}, "NaN", "Infinity", float("-inf"), "0", "-2"):
            with self.subTest(price=price):
                with patch.object(api.requests, "get", return_value=self.response(self.cmb_payload(curPrice=price))):
                    self.assertFalse(api._fetch_cmb(self.now)["ok"])

    def test_cmb_missing_daily_reference_does_not_fabricate_zero_change(self):
        for fields in ({"preClose": "NaN"}, {"preClose": "0"}, {"upDown": None},
                       {"preClose": "1e-323", "upDown": "1000"}):
            with self.subTest(fields=fields):
                with patch.object(api.requests, "get", return_value=self.response(self.cmb_payload(**fields))):
                    result = api._fetch_cmb(self.now)
                self.assertTrue(result["ok"])
                self.assertIsNone(result["data"]["change"])
                self.assertIsNone(result["data"]["change_pct"])

    def test_swissquote_accepts_numeric_strings_and_normalizes_milliseconds(self):
        with patch.object(api.requests, "get", return_value=self.response(self.sq_payload())):
            quote = api._sq_quote(api.SQ_GOLD_URL)
        self.assertEqual(quote["price"], 2401.0)
        self.assertEqual(quote["timestamp"], self.now.timestamp())

    def test_swissquote_rejects_invalid_quotes(self):
        payloads = [None, {}, [], [None], [{}], [{"spreadProfilePrices": [None]}],
                    self.sq_payload(bid="NaN"), self.sq_payload(ask="Infinity"),
                    self.sq_payload(bid=True), self.sq_payload(bid="0"),
                    self.sq_payload(bid="2403", ask="2402"), self.sq_payload(timestamp="NaN")]
        for payload in payloads:
            with self.subTest(payload=payload):
                with patch.object(api.requests, "get", return_value=self.response(payload)):
                    self.assertIsNone(api._sq_quote(api.SQ_GOLD_URL))

    def test_international_quote_never_fetches_or_uses_cmb_daily_baseline(self):
        quotes = [
            {"price": 2401.0, "timestamp": self.now.timestamp()},
            {"price": 7.2, "timestamp": self.now.timestamp() - 20},
        ]
        with patch.object(api, "_sq_quote", side_effect=quotes), patch.object(api, "_fetch_cmb") as cmb:
            result = api._fetch_source("intl", self.now)
        cmb.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["price"], 555.80)
        self.assertIsNone(result["data"]["change"])
        self.assertIsNone(result["data"]["change_pct"])
        self.assertEqual(result["data"]["quote_timestamp"], self.now.timestamp() - 20)

    def test_international_quote_validates_market_hours_after_requests(self):
        quote = {"price": 100.0, "timestamp": self.now.timestamp()}
        with patch.object(api, "_sq_quote", return_value=quote), \
                patch.object(api, "_is_intl_trading_time", side_effect=[True, False]):
            result = api._fetch_swissquote(self.now)
        self.assertFalse(result["ok"])
        self.assertIn("outside trading hours", result["error"])

    def test_international_quote_rejects_stale_exchange_rate(self):
        quotes = [
            {"price": 2401.0, "timestamp": self.now.timestamp()},
            {"price": 7.2, "timestamp": self.now.timestamp() - 181},
        ]
        with patch.object(api, "_sq_quote", side_effect=quotes):
            result = api._fetch_swissquote(self.now)
        self.assertFalse(result["ok"])
        self.assertIn("USD/CNH quote is stale", result["error"])

    def test_international_quote_rejects_overflow_in_converted_price(self):
        quote = {"price": 1e308, "timestamp": self.now.timestamp()}
        with patch.object(api, "_sq_quote", return_value=quote):
            result = api._fetch_swissquote(self.now)
        self.assertFalse(result["ok"])
        self.assertIn("converted price is invalid", result["error"])

    def test_invalid_primary_payload_falls_back_to_international(self):
        health = {"cmb": 0.0, "intl": 0.0}
        responses = [self.response(None), self.response(self.sq_payload()),
                     self.response(self.sq_payload(bid="7.19", ask="7.21"))]
        with patch.dict(api._source_unhealthy_until, health, clear=True), \
                patch.object(api, "_last_success_source", None), \
                patch.object(api.requests, "get", side_effect=responses) as request:
            result = api.fetch_gold_price_result(now=self.now)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["source"], "intl")
        self.assertEqual(result["data"]["fallback_from"], "cmb")
        self.assertEqual(request.call_count, 3)


if __name__ == "__main__":
    unittest.main()

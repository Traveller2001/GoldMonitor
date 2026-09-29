import json
import unittest
from datetime import datetime, timezone

import macro_sources as ms

NOW = datetime(2026, 9, 29, 1, 30, tzinfo=timezone.utc).timestamp()


def kalshi_market(suffix, price, prev=None, bid=None, ask=None, status="active", close="2026-10-28T17:59:00Z"):
    market = {"ticker": f"KXFEDDECISION-26OCT-{suffix}", "status": status, "close_time": close,
              "last_price_dollars": f"{price:.4f}"}
    if prev is not None:
        market["previous_price_dollars"] = f"{prev:.4f}"
    if bid is not None:
        market.update(yes_bid_dollars=f"{bid:.4f}", yes_ask_dollars=f"{ask:.4f}")
    return market


class NumberTest(unittest.TestCase):
    def test_leading_number_keeps_source_units(self):
        for raw, expected in (("16.2", 16.2), ("-2.3", -2.3), ("98K", 98.0), ("4.1%", 4.1),
                              ("1,234.5", 1234.5), ("−0.1", -0.1), (3, 3.0)):
            with self.subTest(raw=raw):
                self.assertEqual(ms.parse_number(raw), expected)
        for raw in (None, True, "", "--", "abc", float("nan"), float("inf")):
            with self.subTest(raw=raw):
                self.assertIsNone(ms.parse_number(raw))


class FedExpectationTest(unittest.TestCase):
    def test_kalshi_uses_tight_quotes_normalizes_and_reads_previous_day(self):
        payload = {"events": [{"strike_date": "2026-10-28T18:00:00Z", "markets": [
            kalshi_market("C26", 0.01, 0.01), kalshi_market("C25", 0.01, 0.01),
            kalshi_market("H0", 0.29, 0.33, bid=0.29, ask=0.30),
            kalshi_market("H25", 0.68, 0.67, bid=0.68, ask=0.70),
            kalshi_market("H26", 0.01, 0.01),
        ]}]}
        fed = ms.parse_kalshi_events(payload, NOW)
        self.assertEqual([o.bps for o in fed.outcomes], [-50, -25, 0, 25, 50])
        self.assertAlmostEqual(sum(o.prob for o in fed.outcomes), 1.0)
        # (0.69 mid + 0.01) / 1.015 after normalizing the five quotes
        self.assertAlmostEqual(fed.probability("hike"), 0.6897, places=3)
        self.assertGreater(fed.expected_bps, fed.prev_expected_bps)
        self.assertEqual(fed.headline(), "10月加息 69%")
        self.assertEqual(datetime.fromtimestamp(fed.meeting_ts, ms._NEW_YORK).strftime("%m-%d %H:%M"), "10-28 14:00")

    def test_kalshi_picks_the_nearest_open_meeting_and_legacy_cent_fields(self):
        later = {"markets": [kalshi_market("H0", 0.9, close="2026-12-09T18:59:00Z")]}
        past = {"markets": [kalshi_market("H0", 0.9, close="2026-09-16T17:59:00Z")]}
        nearest = {"markets": [
            {"ticker": "X-C25", "status": "active", "close_time": "2026-10-28T17:59:00Z", "last_price": 80},
            {"ticker": "X-H0", "status": "active", "close_time": "2026-10-28T17:59:00Z", "last_price": 20},
            {"ticker": "X-H25", "status": "settled", "close_time": "2026-10-28T17:59:00Z", "last_price": 99},
        ]}
        fed = ms.parse_kalshi_events({"events": [later, past, nearest]}, NOW)
        self.assertEqual([o.bps for o in fed.outcomes], [-25, 0])
        self.assertAlmostEqual(fed.probability("cut"), 0.8)
        self.assertIsNone(fed.prev_expected_bps)

    def test_kalshi_rejects_payloads_without_usable_prices(self):
        for payload in (None, {}, {"events": []}, {"events": [{"markets": [kalshi_market("H0", 0.0)]}]}):
            with self.subTest(payload=payload):
                with self.assertRaises(ms.SourceError):
                    ms.parse_kalshi_events(payload, NOW)

    def test_polymarket_decodes_string_prices_and_fills_tail_changes(self):
        payload = [
            {"title": "Fed Decision in December?", "endDate": "2026-12-10T04:59:00Z", "markets": []},
            {"title": "Fed Decision in September?", "endDate": "2026-09-17T03:59:00Z", "markets": []},
            {"title": "Fed Decision in October?", "endDate": "2026-10-29T03:59:00Z", "markets": [
                {"groupItemTitle": "50+ bps decrease", "outcomePrices": json.dumps(["0.0025", "0.9975"])},
                {"groupItemTitle": "No change", "outcomePrices": ["0.305", "0.695"], "oneDayPriceChange": -0.03,
                 "bestBid": 0.30, "bestAsk": 0.31},
                {"groupItemTitle": "25 bps increase", "outcomePrices": ["0.685", "0.315"], "oneDayPriceChange": 0.04,
                 "bestBid": 0.68, "bestAsk": 0.69},
                {"groupItemTitle": "25 bps increase", "closed": True, "outcomePrices": ["1", "0"]},
            ]},
        ]
        fed = ms.parse_polymarket_events(payload, NOW)
        self.assertEqual(fed.meeting_month, 10)
        self.assertEqual([o.bps for o in fed.outcomes], [-50, 0, 25])
        self.assertIsNotNone(fed.prev_expected_bps)
        self.assertGreater(fed.expected_bps, fed.prev_expected_bps)

    def test_fedwatch_bulletin_uses_its_nearest_future_meeting(self):
        text = ("CME美联储观察：美联储到10月维持利率不变的概率为31.2%，累计加息25个基点的概率为68.8%。"
                "美联储到12月维持利率不变的概率为20.1%，累计加息25个基点的概率为51.8%，累计加息50个基点的概率为28.1%。")
        fed = ms.parse_fedwatch_text(text, NOW - 3600, NOW)
        self.assertEqual(fed.meeting_month, 10)
        self.assertEqual([(o.bps, round(o.prob, 3)) for o in fed.outcomes], [(0, 0.312), (25, 0.688)])

    def test_fedwatch_bulletin_rejects_stale_or_incomplete_text(self):
        passed = "CME美联储观察：美联储到9月维持利率不变的概率为65.2%，累计加息25个基点的概率为34.8%。"
        self.assertIsNone(ms.parse_fedwatch_text(passed, NOW - 3600, NOW))
        fresh = "CME美联储观察：美联储到10月维持利率不变的概率为31.2%，累计加息25个基点的概率为68.8%。"
        self.assertIsNone(ms.parse_fedwatch_text(fresh, NOW - 4 * 86400, NOW))
        partial = "CME美联储观察：美联储到10月维持利率不变的概率为31.2%。"
        self.assertIsNone(ms.parse_fedwatch_text(partial, NOW - 3600, NOW))


class WallstreetcnTest(unittest.TestCase):
    def test_calendar_keeps_raw_text_and_numbers(self):
        payload = {"data": {"items": [
            {"id": 1589212, "public_date": 1788525000, "country_id": "US", "title": "8月非农就业人口变动(万人)",
             "event": "非农就业人口变动", "unit": "万人", "importance": 4, "calendar_type": "FD",
             "actual": "16.2", "forecast": "5.5", "previous": "-2.3", "uri": "https://wallstreetcn.com/calendar/x"},
            {"id": 15250, "public_date": 1790049720, "country_id": "US", "title": "美联储官员讲话",
             "event": "", "importance": 3, "calendar_type": "FE", "actual": "", "forecast": ""},
            {"id": 3, "public_date": None, "title": "missing time"}, None,
        ]}}
        items = ms.parse_wscn_calendar(payload)
        self.assertEqual(len(items), 2)
        nfp, speech = items
        self.assertEqual((nfp.actual, nfp.forecast, nfp.previous, nfp.kind), (16.2, 5.5, -2.3, "data"))
        self.assertEqual((nfp.actual_text, nfp.unit, nfp.importance), ("16.2", "万人", 4))
        self.assertEqual((speech.kind, speech.event, speech.actual), ("event", "美联储官员讲话", None))

    def test_news_strips_html_and_uses_score_as_importance(self):
        payload = {"data": {"items": [
            {"id": 1, "display_time": 1790616348, "title": "", "content": "<p>美联储<em>理事</em>库克：&amp;通胀</p>",
             "score": 2, "uri": "u"},
            {"id": 2, "display_time": 1790616000, "content_text": ""},
        ]}}
        items = ms.parse_wscn_news(payload, "华尔街见闻")
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0].text, items[0].importance, items[0].id), ("美联储理事库克：&通胀", 2, "wscn:1"))

    def test_malformed_envelope_raises_source_error(self):
        for payload in (None, [], {"data": None}, {"data": {"items": {}}}):
            with self.subTest(payload=payload):
                with self.assertRaises(ms.SourceError):
                    ms.parse_wscn_calendar(payload)


class SeriesTest(unittest.TestCase):
    def test_fred_csv_skips_missing_values_and_measures_change(self):
        text = "observation_date,DFII10\n2026-09-18,2.41\n2026-09-21,.\n2026-09-22,\n2026-09-23,2.50\n2026-09-25,2.61\n"
        series = ms.parse_fred_csv(text, "DFII10")
        self.assertEqual(series.points, [("2026-09-18", 2.41), ("2026-09-23", 2.5), ("2026-09-25", 2.61)])
        latest, delta, as_of = series.change(2)
        self.assertEqual((latest, as_of), (2.61, "2026-09-25"))
        self.assertAlmostEqual(delta, 0.20)
        self.assertIsNone(series.change(3))
        with self.assertRaises(ms.SourceError):
            ms.parse_fred_csv("DATE,DFII10\n", "DFII10")

    def test_sina_kline_reads_close_from_the_last_bars(self):
        text = ('/*<script>location.href=\'//sina.com\';</script>*/\n'
                'var _DINIW=("2026-09-25,100.8,100.7,101.1,101.0,|2026-09-28,101.0,100.9,101.3,101.207,|");')
        series = ms.parse_sina_kline(text)
        self.assertEqual(series.points, [("2026-09-25", 101.0), ("2026-09-28", 101.207)])
        with self.assertRaises(ms.SourceError):
            ms.parse_sina_kline("<html>blocked</html>")


class ForexFactoryTest(unittest.TestCase):
    def test_only_us_high_and_medium_impact_rows_are_kept(self):
        payload = [
            {"title": "Non-Farm Employment Change", "country": "USD", "date": "2026-10-02T08:30:00-04:00",
             "impact": "High", "forecast": "98K", "previous": "162K"},
            {"title": "SPPI y/y", "country": "JPY", "date": "2026-09-27T19:50:00-04:00", "impact": "Low"},
            {"title": "Bank Holiday", "country": "USD", "date": "2026-10-12T00:00:00-04:00", "impact": "Holiday"},
        ]
        items = ms.parse_forexfactory(payload)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0].event, items[0].forecast_text, items[0].importance), ("非农就业人口变动", "98K", 3))
        self.assertIsNone(items[0].actual)


class FallbackTest(unittest.TestCase):
    def test_first_available_reports_skipped_sources(self):
        def broken():
            raise ms.SourceError("timeout")

        def malformed():
            raise KeyError("x")

        result, errors = ms.first_available((("A", broken), ("B", malformed), ("C", lambda: 42)))
        self.assertEqual(result, 42)
        self.assertEqual(errors, ["A: timeout", "B: 解析失败：KeyError"])
        result, errors = ms.first_available((("A", broken),))
        self.assertIsNone(result)

    def test_fomc_schedule_is_ordered_and_next_meeting_is_future(self):
        times = ms.fomc_decision_times()
        self.assertEqual(times, sorted(times))
        self.assertEqual(datetime.fromtimestamp(ms.next_fomc(NOW), ms._NEW_YORK).date().isoformat(), "2026-10-28")


if __name__ == "__main__":
    unittest.main()

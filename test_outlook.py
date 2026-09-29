import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import MagicMock

import requests

import macro_sources as ms
import outlook
from macro_sources import CalendarItem, FedExpectation, FedOutcome, NewsItem, Series
from news_ai import DeepRead

NOW = datetime(2026, 9, 29, 1, 30, tzinfo=timezone.utc).timestamp()
HOUR = 3600.0


def release(event, actual, forecast, hours_ago=2.0, title=None, item_id=None, unit="%"):
    return CalendarItem(
        id=item_id or f"wscn:{event}", ts=NOW - hours_ago * HOUR, title=title or event, event=event,
        country="US", importance=3, kind="data", unit=unit, actual=actual, forecast=forecast,
        actual_text=f"{actual:g}" if actual is not None else "", forecast_text=f"{forecast:g}",
    )


def fed(expected_hike, previous_hike=None):
    return FedExpectation(ms.next_fomc(NOW), [
        FedOutcome(0, 1 - expected_hike, None if previous_hike is None else 1 - previous_hike),
        FedOutcome(25, expected_hike, previous_hike),
    ], "Kalshi", NOW)


def news(text, hours_ago=1.0, importance=1, item_id=None):
    return NewsItem(item_id or f"n:{text}", NOW - hours_ago * HOUR, text, "", importance, "华尔街见闻")


class ToneTest(unittest.TestCase):
    def test_lexicon_handles_negation_reversal_and_explicit_phrases(self):
        cases = {
            "美联储官员：不排除进一步加息": -1,
            "美联储不急于降息": -1,
            "美联储暂不考虑降息": -1,
            "美联储未来可能降息": 1,
            "市场对美联储加息预期降温": 1,
            "美国8月核心PCE同比上涨3.3%，通胀压力减弱": 1,
            "白宫顾问：核心通胀接近美联储2%目标": 1,
            "10年期美债收益率涨7.56个基点": -1,
            "避险情绪降温，金价回落": -1,
            "现货黄金下跌4.01%": 0,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(outlook.classify_tone(text), expected)

    def test_relevance_excludes_other_central_banks_schedules_and_bulletins(self):
        self.assertTrue(outlook.is_relevant("克利夫兰联储主席：长期收益率上升"))
        self.assertTrue(outlook.is_relevant("美国8月CPI同比上涨3.4%，通胀仍高"))
        self.assertFalse(outlook.is_relevant("澳洲联储宣布降息25个基点"))
        self.assertFalse(outlook.is_relevant("① 12:30 澳洲联储利率决议；② 22:00 美国JOLTS"))
        self.assertFalse(outlook.is_relevant("CME美联储观察：美联储到10月维持利率不变的概率为31%"))
        self.assertFalse(outlook.is_relevant("伊朗外长发表讲话"))


class FactorTest(unittest.TestCase):
    def test_hawkish_pricing_is_bearish_and_rising_hawkishness_adds_to_it(self):
        steady = outlook.score_fed(fed(0.68, 0.68))
        rising = outlook.score_fed(fed(0.68, 0.40))
        easing = outlook.score_fed(FedExpectation(ms.next_fomc(NOW), [FedOutcome(-25, 0.8), FedOutcome(0, 0.2)], "Kalshi", NOW))
        self.assertLess(steady.signal, 0)
        self.assertLess(rising.signal, steady.signal)
        self.assertGreater(easing.signal, 0.5)
        self.assertIn("24h", rising.value)
        missing = outlook.score_fed(None, ["Kalshi: timeout"])
        self.assertFalse(missing.available)
        self.assertIn("timeout", missing.note)

    def test_data_surprises_follow_their_rate_direction(self):
        nfp_beat, _ = outlook.score_data([release("非农就业人口变动", 16.2, 5.5, unit="万人")], NOW, True)
        jobless_up, _ = outlook.score_data([release("失业率", 4.3, 4.1)], NOW, True)
        in_line, releases = outlook.score_data([release("CPI同比", 3.4, 3.4)], NOW, True)
        self.assertLess(nfp_beat.signal, -0.6)
        self.assertGreater(jobless_up.signal, 0.4)
        self.assertEqual(in_line.signal, 0.0)
        self.assertEqual(releases[0].verdict, "符合预期")

    def test_old_final_and_unknown_releases_count_less_or_not_at_all(self):
        fresh, _ = outlook.score_data([release("非农就业人口变动", 16.2, 5.5, unit="万人")], NOW, True)
        aged, _ = outlook.score_data([release("非农就业人口变动", 16.2, 5.5, hours_ago=96, unit="万人")], NOW, True)
        final, _ = outlook.score_data([release("实际GDP年化季环比", 2.5, 1.5, title="二季度实际GDP终值")], NOW, True)
        prelim, _ = outlook.score_data([release("实际GDP年化季环比", 2.5, 1.5)], NOW, True)
        ignored, releases = outlook.score_data([
            release("达拉斯联储商业活动指数", 9.8, 7.8), release("非农就业人口变动", 16.2, 5.5, hours_ago=12 * 24),
        ], NOW, True)
        self.assertLess(abs(aged.signal), abs(fresh.signal))
        self.assertLess(abs(final.signal), abs(prelim.signal))
        self.assertEqual((ignored.signal, releases), (0.0, []))
        unavailable, _ = outlook.score_data([], NOW, False)
        self.assertFalse(unavailable.available)

    def test_rising_real_yields_and_dollar_are_bearish(self):
        rates = Series("DFII10", [(f"2026-09-{d}", v) for d, v in ((18, 2.41), (21, 2.45), (22, 2.47),
                                                                    (23, 2.50), (24, 2.55), (25, 2.61))])
        factor = outlook.score_series("rates", (rates, "FRED DFII10"))
        self.assertLess(factor.signal, -0.8)
        self.assertIn("+20bp", factor.value)
        self.assertIn("截至 09-25", factor.note)
        dollar = Series("DINIW", [(f"2026-09-{d}", v) for d, v in ((21, 101.0), (22, 100.8), (23, 100.6),
                                                                   (24, 100.5), (25, 100.4), (28, 100.0))])
        self.assertGreater(outlook.score_series("usd", (dollar, "新浪")).signal, 0.7)
        short = Series("DFII10", [("2026-09-25", 2.61)])
        self.assertFalse(outlook.score_series("rates", (short, "FRED")).available)
        self.assertFalse(outlook.score_series("usd", None).available)

    def test_ai_tags_override_the_lexicon_and_recent_items_weigh_more(self):
        items = [news("美联储理事：通胀顽固", item_id="a"), news("美联储官员讲话", item_id="b")]
        rule = outlook.tag_headlines(items, NOW, {})
        tagged = outlook.tag_headlines(items, NOW, {"b": 1})
        self.assertEqual([(h.item.id, h.tone, h.by) for h in rule], [("a", -1, "词典"), ("b", 0, "词典")])
        self.assertEqual(tagged[1].by, "AI")
        self.assertLess(outlook.score_news(rule, NOW, True).signal, 0)
        self.assertEqual(outlook.score_news(tagged, NOW, True).signal, 0.0)
        old = outlook.tag_headlines([news("美联储理事：通胀顽固", hours_ago=30)], NOW, {})
        self.assertEqual(outlook.score_news(old, NOW, True).signal, 0.0)

    def test_missing_factors_count_as_zero_and_lower_confidence(self):
        full = [outlook.Factor(key, name, weight, signal=-0.8) for key, name, weight in outlook.FACTOR_SPECS]
        score, stance, tone, confidence = outlook.combine(full)
        self.assertEqual((score, stance, tone, confidence), (-80, "明显偏空", -1, "高"))
        partial = [outlook.Factor(key, name, weight, signal=-0.8 if key == "fed" else None)
                   for key, name, weight in outlook.FACTOR_SPECS]
        self.assertEqual(outlook.combine(partial)[0], -28)
        self.assertEqual(outlook.combine(partial)[3], "低")
        mixed = [outlook.Factor(key, name, weight, signal=0.8 if key in ("fed", "data") else -0.8)
                 for key, name, weight in outlook.FACTOR_SPECS]
        self.assertEqual(outlook.combine(mixed)[3], "中")
        sparse = [outlook.Factor(key, name, weight, signal=0.9 if key == "news" else None)
                  for key, name, weight in outlook.FACTOR_SPECS]
        self.assertEqual(outlook.combine(sparse)[:2], (None, "数据不足"))


class ScheduleTest(unittest.TestCase):
    def test_upcoming_keeps_pending_us_items_and_always_the_next_fomc(self):
        pending = release("非农就业人口变动", None, 9.8, hours_ago=-80, unit="万人")
        done = release("失业率", 4.1, 4.1, hours_ago=1)
        foreign = CalendarItem("x", NOW + HOUR, "欧元区CPI", "CPI同比", "EA", 3, "data")
        speech = CalendarItem("s", NOW + 2 * HOUR, "美联储官员密集发声", "美联储官员密集发声", "US", 3, "event")
        items = outlook.upcoming_events([pending, done, foreign, speech], NOW)
        self.assertEqual([item.event for item in items], ["美联储官员密集发声", "非农就业人口变动", "FOMC利率决议"])

    def test_refresh_speeds_up_around_key_releases(self):
        report = MagicMock(upcoming=[release("非农就业人口变动", None, 9.8, hours_ago=-0.5, unit="万人")])
        self.assertEqual(outlook.next_refresh_delay(report, NOW), 300.0)  # 30 min away: normal cadence
        soon = MagicMock(upcoming=[release("非农就业人口变动", None, 9.8, hours_ago=-(60 / 3600), unit="万人")])
        self.assertAlmostEqual(outlook.next_refresh_delay(soon, NOW), 80.0)
        waiting = MagicMock(upcoming=[release("非农就业人口变动", None, 9.8, hours_ago=0.05, unit="万人")])
        self.assertEqual(outlook.next_refresh_delay(waiting, NOW), 30.0)
        minor = MagicMock(upcoming=[release("首次申请失业救济人数", None, 20.0, hours_ago=-(60 / 3600))])
        self.assertEqual(outlook.next_refresh_delay(minor, NOW), 300.0)


class FakeResponse:
    def __init__(self, payload=None, text="", status=200):
        self._payload, self.text, self.status_code = payload, text, status

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeWeb:
    """Routes requests by URL; a missing route behaves like a blocked host."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        route = self.routes.get(url)
        if route is None:
            raise requests.ConnectionError("blocked")
        return route(params or {})


def kalshi_payload():
    return {"events": [{"strike_date": "2026-10-28T18:00:00Z", "markets": [
        {"ticker": "E-H0", "status": "active", "close_time": "2026-10-28T17:59:00Z", "last_price_dollars": "0.30",
         "previous_price_dollars": "0.35"},
        {"ticker": "E-H25", "status": "active", "close_time": "2026-10-28T17:59:00Z", "last_price_dollars": "0.70",
         "previous_price_dollars": "0.65"},
    ]}]}


def calendar_payload(actual=None):
    return {"data": {"items": [
        {"id": 7, "public_date": NOW - 60, "country_id": "US", "title": "9月非农就业人口变动(万人)",
         "event": "非农就业人口变动", "unit": "万人", "importance": 4, "calendar_type": "FD",
         "actual": actual or "", "forecast": "9.8", "previous": "16.2"},
    ]}}


def news_payload():
    return {"data": {"items": [
        {"id": 1, "display_time": NOW - 600, "content_text": "美联储理事：通胀顽固，不排除进一步加息", "score": 2},
        {"id": 2, "display_time": NOW - 900, "content_text": "伊朗外长发表讲话", "score": 1},
    ]}}


FRED_TEXT = "observation_date,DFII10\n" + "\n".join(
    f"2026-09-{day},{value}" for day, value in ((18, 2.41), (21, 2.45), (22, 2.47), (23, 2.5), (24, 2.55), (25, 2.61)))
SINA_TEXT = 'var _DINIW=("' + "|".join(
    f"2026-09-{day},1,1,1,{value}," for day, value in ((21, 100.2), (22, 100.5), (23, 100.8), (24, 100.9),
                                                         (25, 101.0), (28, 101.2))) + '");'


class MacroServiceTest(unittest.TestCase):
    def service(self, routes, clock=None, analyst=None):
        self.web = FakeWeb(routes)
        return outlook.MacroService(analyst=analyst, http_get=self.web, clock=clock or (lambda: NOW))

    def full_routes(self, actual=None):
        return {
            ms.KALSHI_EVENTS_URL: lambda p: FakeResponse(kalshi_payload()),
            ms.WSCN_CALENDAR_URL: lambda p: FakeResponse(calendar_payload(actual)),
            ms.WSCN_LIVES_URL: lambda p: FakeResponse(news_payload()),
            ms.WSCN_SEARCH_URL: lambda p: FakeResponse({"data": {"items": []}}),
            ms.FRED_CSV_URL: lambda p: FakeResponse(text=FRED_TEXT),
            ms.SINA_DXY_URL: lambda p: FakeResponse(text=SINA_TEXT),
        }

    def test_refresh_combines_every_source_into_a_bearish_outlook(self):
        report = self.service(self.full_routes()).refresh()
        self.assertEqual(report.available_factors, 5)
        self.assertLess(report.score, -15)
        self.assertEqual(report.fed.source, "Kalshi")
        self.assertEqual([h.item.id for h in report.headlines], ["wscn:1"])
        self.assertEqual(report.upcoming[0].event, "非农就业人口变动")
        self.assertEqual(report.errors, [])

    def test_blocked_sources_fall_back_in_order_and_are_reported(self):
        routes = self.full_routes()
        del routes[ms.KALSHI_EVENTS_URL]
        del routes[ms.WSCN_CALENDAR_URL]
        del routes[ms.FRED_CSV_URL]
        routes[ms.POLYMARKET_EVENTS_URL] = lambda p: FakeResponse([{
            "title": "Fed Decision in October?", "endDate": "2026-10-29T03:59:00Z",
            "markets": [{"groupItemTitle": "No change", "outcomePrices": ["0.4", "0.6"]},
                        {"groupItemTitle": "25 bps increase", "outcomePrices": ["0.6", "0.4"]}]}])
        routes[ms.FOREXFACTORY_URL] = lambda p: FakeResponse([{
            "title": "Unemployment Rate", "country": "USD", "date": "2026-10-02T08:30:00-04:00", "impact": "High",
            "forecast": "4.1%", "previous": "4.1%"}])
        report = self.service(routes).refresh()
        self.assertEqual(report.fed.source, "Polymarket")
        self.assertFalse(report.factors[1].available)  # no actual values without WallstreetCN
        self.assertIn("失业率", [item.event for item in report.upcoming])
        self.assertFalse(report.factors[2].available)
        self.assertTrue(any(error.startswith("Kalshi") for error in report.errors))
        self.assertTrue(any("FRED DGS10" in error for error in report.errors))

    def test_failed_source_rests_before_the_next_attempt(self):
        now = [NOW]
        routes = self.full_routes()
        del routes[ms.KALSHI_EVENTS_URL]
        routes[ms.WSCN_SEARCH_URL] = lambda p: FakeResponse({"data": {"items": [{
            "id": 9, "display_time": NOW - 600,
            "content_text": "CME美联储观察：美联储到10月维持利率不变的概率为30%，累计加息25个基点的概率为70%。"}]}})
        service = self.service(routes, clock=lambda: now[0])
        self.assertTrue(service.refresh().fed.source.startswith("CME"))
        tried = lambda: [call for call in self.web.calls if call[0] in (ms.KALSHI_EVENTS_URL, ms.POLYMARKET_EVENTS_URL)]
        self.assertEqual(len(tried()), 2)
        now[0] += 300
        report = service.refresh()
        self.assertEqual(len(tried()), 2)  # both still resting
        self.assertTrue(all("不再重试" in error for error in report.errors if error.startswith(("Kalshi", "Poly"))))
        now[0] += outlook.SOURCE_COOLDOWN
        service.refresh()
        self.assertEqual(len(tried()), 4)

    def test_series_are_cached_between_refreshes(self):
        now = [NOW]
        service = self.service(self.full_routes(), clock=lambda: now[0])
        service.refresh()
        now[0] += 600
        service.refresh()
        fred_calls = [call for call in self.web.calls if call[0] == ms.FRED_CSV_URL]
        self.assertEqual(len(fred_calls), 1)
        self.assertEqual(fred_calls[0][1]["id"], "DFII10")
        now[0] += 600  # the dollar index goes stale after 15 minutes, daily FRED data after an hour
        service.refresh()
        self.assertEqual(len([call for call in self.web.calls if call[0] == ms.SINA_DXY_URL]), 2)
        self.assertEqual(len([call for call in self.web.calls if call[0] == ms.FRED_CSV_URL]), 1)

    def test_new_key_release_is_reported_once(self):
        now = [NOW]
        routes = self.full_routes()
        service = self.service(routes, clock=lambda: now[0])
        self.assertEqual(service.refresh().new_releases, [])
        routes[ms.WSCN_CALENDAR_URL] = lambda p: FakeResponse(calendar_payload(actual="25.0"))
        now[0] += 30
        report = service.refresh()
        self.assertEqual([r.label for r in report.new_releases], ["非农"])
        self.assertLess(report.releases[0].impact, 0)
        now[0] += 30
        self.assertEqual(service.refresh().new_releases, [])

    def test_ai_plan_tags_new_headlines_and_triggers_deep_read_on_releases(self):
        analyst = MagicMock(active=True, last_deep=None)
        analyst.cached_tags.return_value = {}
        analyst.status_text.return_value = ""
        analyst.tag_ready.return_value = True
        analyst.deep_ready.return_value = True
        analyst.pending.side_effect = lambda items: list(items)
        service = self.service(self.full_routes(), analyst=analyst)
        report = service.refresh()
        job = service.plan_ai(report)
        self.assertEqual([item.id for item in job.headlines], ["wscn:1"])
        self.assertEqual(job.deep_trigger, "定时解读")
        self.assertIn("市场定价 10月加息", job.context)
        analyst.cached_tags.return_value = {"wscn:1": 1}
        analyst.last_deep = DeepRead(at=NOW, stance="偏空", summary="s", trigger="t", expected_bps=report.fed.expected_bps,
                                     score=report.score)
        updated = service.run_ai(job)
        analyst.tag_headlines.assert_called_once()
        brief = analyst.deep_read.call_args[0][0]
        self.assertIn("规则评分", brief)
        self.assertIn("利率预期", brief)
        self.assertLess(len(brief), 3000)
        self.assertEqual(updated.headlines[0].by, "AI")
        # Same pricing and score as the last deep read, nothing new to tag: no further calls.
        analyst.pending.side_effect = lambda items: []
        self.assertIsNone(service.plan_ai(updated))

    def test_key_release_read_waits_out_a_cooldown_instead_of_being_dropped(self):
        now = [NOW]
        analyst = MagicMock(active=True)
        analyst.cached_tags.return_value = {}
        analyst.status_text.return_value = ""
        analyst.tag_ready.return_value = False
        analyst.deep_ready.return_value = False
        routes = self.full_routes()
        service = self.service(routes, clock=lambda: now[0], analyst=analyst)
        report = service.refresh()
        analyst.last_deep = DeepRead(at=NOW, stance="偏空", summary="s", trigger="t",
                                     expected_bps=report.fed.expected_bps, score=report.score)
        routes[ms.WSCN_CALENDAR_URL] = lambda p: FakeResponse(calendar_payload(actual="25.0"))
        now[0] += 30
        self.assertIsNone(service.plan_ai(service.refresh()))  # an earlier read is still cooling down
        analyst.deep_ready.assert_called_with(False, release=True)
        now[0] += 30
        analyst.deep_ready.return_value = True
        job = service.plan_ai(service.refresh())
        self.assertEqual(job.deep_trigger, "数据公布：非农")
        updated = service.run_ai(job)
        self.assertTrue(analyst.deep_read.call_args.kwargs["release"])
        analyst.last_deep = replace(analyst.last_deep, at=now[0], score=updated.score)  # the read landed
        self.assertIsNone(service.plan_ai(updated))  # the print is covered now

    def test_deep_triggers_ignore_boundary_noise_and_closed_markets(self):
        analyst = MagicMock(last_deep=None)
        service = outlook.MacroService(analyst=analyst)
        view = lambda score, fed=None: MagicMock(score=score, tone=outlook.stance_for(score)[1], fed=fed,
                                                 generated_at=NOW)
        self.assertEqual(service._deep_trigger(view(0), False), "定时解读")
        self.assertEqual(service._deep_trigger(view(0), False, market_open=False), "")
        analyst.last_deep = DeepRead(at=NOW - 9 * HOUR, stance="中性", summary="s", score=13)
        self.assertEqual(service._deep_trigger(view(13), False), "定时解读")
        self.assertEqual(service._deep_trigger(view(13), False, market_open=False), "")
        analyst.last_deep.at = NOW - HOUR
        self.assertEqual(service._deep_trigger(view(16), False), "")  # neutral -> bullish by 3 points
        self.assertEqual(service._deep_trigger(view(21), False), "评分方向变化")
        analyst.last_deep.score = -20
        self.assertEqual(service._deep_trigger(view(-35), False), "")
        self.assertEqual(service._deep_trigger(view(-41), False), "评分变化 -21")
        analyst.last_deep.expected_bps = 10.0
        self.assertEqual(service._deep_trigger(view(-20, MagicMock(expected_bps=16.0)), False), "利率预期变化 +6bp")
        self.assertEqual(service._deep_trigger(view(-20), False, releases=["非农", "失业率"]), "数据公布：非农、失业率")

    def test_manual_request_always_asks_for_a_deep_read(self):
        analyst = MagicMock(active=True, last_deep=None)
        analyst.cached_tags.return_value = {}
        analyst.tag_ready.return_value = False
        analyst.deep_ready.return_value = True
        service = self.service(self.full_routes(), analyst=analyst)
        report = service.refresh()
        self.assertEqual(service.plan_ai(report, manual=True).deep_trigger, "手动请求")
        analyst.active = False
        self.assertIsNone(service.plan_ai(report, manual=True))


if __name__ == "__main__":
    unittest.main()

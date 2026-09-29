import os
import time
import unittest
from dataclasses import replace
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint, QSize, QTimer
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtWidgets import QApplication, QMenu

import main
from macro_sources import CalendarItem, FedExpectation, FedOutcome, NewsItem
from news_ai import DeepRead
from outlook import Snapshot, build_outlook
from outlook_panel import OutlookDialog
from settings import DEFAULT_CONFIG


class WidgetTestBase:
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        analyst = MagicMock(active=False, last_deep=None)
        analyst.status_text.return_value = ""
        analyst.cached_tags.return_value = {}
        for patcher in (
            patch.object(main, "load_config", return_value=dict(DEFAULT_CONFIG)),
            patch.object(main, "append_log"),
            patch.object(main, "NotificationDispatcher"),
            patch.object(main, "NewsAnalyst", return_value=analyst),
            patch.object(main.GoldWidget, "_init_tray", lambda widget: setattr(widget, "tray", MagicMock())),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        with patch.object(main.GoldWidget, "_fetch_price"):
            self.widget = main.GoldWidget()
        for timer in (self.widget.timer, self.widget._source_transition_timer, self.widget._status_timer,
                      self.widget._macro_timer):
            timer.stop()
        self.widget._macro = MagicMock()
        self.widget._macro.plan_ai.return_value = None

    def tearDown(self):
        if self.widget._morph is not None:
            self.widget._morph.close()
        if self.widget._outlook_dialog is not None:
            self.widget._outlook_dialog.force_close()
        self.widget._fetcher = None
        self.widget._closing = True
        self.widget.hide()
        self.widget.deleteLater()
        self.app.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def quote(self, price=800, source="cmb", change_pct=0.0, timestamp=None):
        return {"ok": True, "data": {
            "price": price, "source": source, "change": 0.0,
            "change_pct": change_pct, "high": 810, "low": 790,
            "quote_timestamp": time.time() if timestamp is None else timestamp,
        }}


class GoldWidgetTest(WidgetTestBase, unittest.TestCase):

    def test_error_retains_price_but_marks_it_as_not_live_then_recovers(self):
        self.widget._on_price(self.quote())
        self.widget._on_price({"ok": False, "error": "timeout"})
        self.assertEqual(self.widget.price_label.text(), "800.00")
        self.assertIn("更新失败", self.widget.status_label.text())
        self.assertEqual(self.widget.interval_label.text(), "--")
        self.assertIn("timeout", self.widget.status_label.toolTip())
        self.widget._on_price(self.quote(price=801))
        self.assertIn("实时", self.widget.status_label.text())
        self.assertEqual(self.widget.price_label.text(), "801.00")
        self.assertNotIn("timeout", self.widget.status_label.toolTip())

    def test_closed_initial_state_never_displays_a_zero_price(self):
        self.widget._on_price({"ok": False, "status": "closed", "error": "closed"})
        self.assertEqual(self.widget.price_label.text(), "--")
        self.assertIn("休市", self.widget.status_label.text())

    def test_older_quote_cannot_roll_back_display_or_trigger_notifications(self):
        stamp = time.time()
        self.widget._on_price(self.quote(price=800, timestamp=stamp))
        with patch.object(self.widget, "_check_notify") as notify:
            self.widget._on_price(self.quote(price=900, timestamp=stamp - 30))
        self.assertEqual(self.widget.last_price, 800)
        self.assertEqual(self.widget.price_label.text(), "800.00")
        self.assertEqual(self.widget._quote_timestamp, stamp)
        notify.assert_not_called()

    def test_repeated_valid_quote_can_recover_after_a_failed_request(self):
        quote = self.quote()
        self.widget._on_price(quote)
        self.widget._on_price({"ok": False, "error": "timeout"})
        self.widget._on_price(quote)
        self.assertIn("实时", self.widget.status_label.text())
        entries = self.widget._price_history.window("cmb", 300)
        self.assertEqual(len(entries), 1)

    def test_quote_age_expires_without_another_fetch_result(self):
        self.widget._on_price(self.quote(timestamp=time.time() - 181))
        self.widget._update_status()
        self.assertIn("报价已过期", self.widget.status_label.text())
        self.assertEqual(self.widget.interval_label.text(), "--")

    def test_zero_daily_change_is_available_and_international_change_is_missing(self):
        self.widget._on_price(self.quote())
        self.assertEqual(self.widget.daily_label.text(), "+0.00%")
        self.widget._on_price(self.quote(source="intl", change_pct=None))
        self.assertEqual(self.widget.daily_label.text(), "--")
        self.assertIn("国际现货", self.widget.source_label.text())

    def test_switching_source_does_not_reuse_other_instrument_history(self):
        now = time.monotonic()
        self.widget._price_history.add("cmb", 700, now=now - 301)
        self.widget._on_price(self.quote(price=800, source="intl", change_pct=None))
        self.assertEqual(self.widget.interval_label.text(), "积累中")

    def test_changing_interval_updates_view_without_waiting_for_network(self):
        now = time.monotonic()
        self.widget._price_history.add("cmb", 800, now=now - 301)
        self.widget._on_price(self.quote(price=808))
        self.assertEqual(self.widget.interval_label.text(), "+1.00%")
        cfg = dict(DEFAULT_CONFIG, interval_minutes=120)
        self.widget._apply_settings(cfg)
        self.assertEqual(self.widget.interval_title.text(), "120分")
        self.assertEqual(self.widget.interval_label.text(), "积累中")

    def test_busy_refreshes_coalesce_and_thread_is_released_after_finished(self):
        with patch.object(main, "PriceFetcher") as factory:
            fetcher = factory.return_value
            self.widget._fetch_price()
            self.widget._fetch_price()
            self.widget._fetch_price()
            self.assertEqual(factory.call_count, 1)
            self.assertTrue(self.widget._fetch_pending)
            self.assertFalse(self.widget.refresh_button.isEnabled())
            with patch.object(main.QTimer, "singleShot") as schedule:
                self.widget._on_fetch_finished()
            schedule.assert_called_once()
            fetcher.deleteLater.assert_called_once()
            self.assertIsNone(self.widget._fetcher)
            self.assertTrue(self.widget.refresh_button.isEnabled())

    def test_quit_waits_for_worker_without_launching_pending_refresh(self):
        self.widget._fetcher = MagicMock()
        self.widget._fetch_pending = True
        with patch.object(QApplication, "quit") as quit_app:
            self.widget._request_quit()
            quit_app.assert_not_called()
            self.assertFalse(self.widget._fetch_pending)
            self.widget._on_price(self.quote())
            self.assertIsNone(self.widget.last_price)
            self.widget._on_fetch_finished()
            quit_app.assert_called_once()

    def test_app_quit_event_uses_safe_shutdown(self):
        self.widget._fetcher = MagicMock()
        with patch.object(QApplication, "quit") as quit_app:
            self.assertTrue(self.widget.eventFilter(self.app, QEvent(QEvent.Type.Quit)))
            self.assertTrue(self.widget._closing)
            quit_app.assert_not_called()

    def test_failed_notification_is_retried_after_cooldown(self):
        self.widget.cfg["notify_high"] = 850
        self.widget._notifier.send.return_value = True
        with patch.object(main.time, "monotonic", return_value=100):
            self.widget._check_notify(900)
            self.widget._on_notification_finished("high", False)
            self.widget._check_notify(900)
            self.assertEqual(self.widget._notifier.send.call_count, 1)
        with patch.object(main.time, "monotonic", return_value=161):
            self.widget._check_notify(900)
        self.assertEqual(self.widget._notifier.send.call_count, 2)

    def test_context_menu_is_reused(self):
        self.widget.tray_menu = QMenu(self.widget)
        initial = len(self.widget.findChildren(QMenu))
        event = QContextMenuEvent(QContextMenuEvent.Reason.Mouse, QPoint(5, 5), QPoint(5, 5))
        with patch.object(QMenu, "exec"):
            for _ in range(5):
                self.widget.contextMenuEvent(event)
        self.assertEqual(len(self.widget.findChildren(QMenu)), initial)

    def test_hover_timer_only_runs_while_docked_and_visible(self):
        docker = self.widget.docker
        self.widget.show()
        self.assertFalse(docker.hover_timer.isActive())
        docker.edge = "right"
        docker.geo = docker.screen_geometry()
        docker.set_collapsed(True, animate=False)
        self.assertTrue(docker.hover_timer.isActive())
        self.widget.hide()
        self.assertFalse(docker.hover_timer.isActive())

    def test_drag_near_edge_docks_and_collapses_to_a_peek_strip(self):
        docker = self.widget.docker
        self.widget.show()
        geo = docker.screen_geometry()
        self.widget.move(geo.right() - self.widget.width() - 10, geo.y() + 100)
        docker.press()
        docker.drag_started()
        docker.release(QPoint(geo.right() - 5, geo.y() + 120), moved=True)
        docker.animation.stop()
        self.assertEqual(docker.edge, "right")
        self.assertTrue(docker.collapsed)
        self.assertEqual(docker.target_pos("right", True).x(), geo.x() + geo.width() - docker.peek)
        docker.reveal()
        self.assertFalse(docker.collapsed)
        self.assertEqual(self.widget.x(), geo.x() + geo.width() - self.widget.width())

    def test_chart_has_dedicated_space_and_handles_empty_flat_and_gap_data(self):
        self.widget.show()
        self.app.processEvents()
        self.assertGreater(self.widget.chart.y(), self.widget.range_label.geometry().bottom())
        self.assertLess(self.widget.chart.geometry().bottom(), self.widget.status_label.y())
        for entries in ([], [(1, 800), (2, 800)], [(1, 800), (250, 810), (300, 805)]):
            self.widget.chart.set_series(entries, 300, main.QColor("#d7bc7d"), 30, now=300)
            self.assertFalse(self.widget.grab().isNull())


def sample_outlook(new_release=False):
    now = time.time()
    fed = FedExpectation(now + 30 * 86400, [FedOutcome(0, 0.3, 0.35), FedOutcome(25, 0.7, 0.65)], "Kalshi", now)
    nfp = CalendarItem("wscn:1", now - 60, "9月非农就业人口变动(万人)", "非农就业人口变动", "US", 4, "data",
                       unit="万人", actual=25.0, forecast=9.8, actual_text="25", forecast_text="9.8")
    pending = CalendarItem("wscn:2", now + 3600, "失业率", "失业率", "US", 3, "data", unit="%",
                           forecast=4.1, forecast_text="4.1")
    news = [NewsItem("wscn:3", now - 600, "美联储理事：通胀顽固，不排除进一步加息", "https://example.com/n", 2)]
    snapshot = Snapshot(now, fed, [nfp, pending], news, None, None, ["FRED DFII10: 网络请求失败：ConnectionError"],
                        True, True)
    result = build_outlook(snapshot, now, deep=DeepRead(now, "偏空", "加息预期升温", ["非农超预期"], ["避险"], "手动请求"))
    if new_release:
        result.new_releases = list(result.releases)
    return result


class OutlookIntegrationTest(WidgetTestBase, unittest.TestCase):
    def test_outlook_bar_shows_stance_score_and_driver(self):
        report = sample_outlook()
        self.widget._on_macro(report)
        self.assertIn(report.stance, self.widget.outlook_bar.text())
        self.assertIn("10", self.widget.outlook_bar.text())
        self.assertIn("加息", self.widget.outlook_bar.text())
        self.assertTrue(self.widget._macro_timer.isActive())
        self.widget._on_macro({"ok": False, "error": "boom"})
        self.assertIs(self.widget._outlook, report)  # a failed refresh keeps the last reading

    def test_macro_polls_slowly_while_markets_are_closed(self):
        with patch.object(main, "is_market_open", return_value=False), \
                patch.object(main, "seconds_until_next_market_transition", return_value=40 * 3600):
            self.widget._on_macro(sample_outlook())
        self.widget._macro.plan_ai.assert_called_with(self.widget._outlook, market_open=False)
        self.assertEqual(self.widget._macro_timer.interval(), main.MACRO_CLOSED_SECONDS * 1000)
        with patch.object(main, "is_market_open", return_value=False), \
                patch.object(main, "seconds_until_next_market_transition", return_value=600):
            self.widget._on_macro(sample_outlook())
        self.assertEqual(self.widget._macro_timer.interval(), 605 * 1000)  # back for the open
        with patch.object(main, "is_market_open", return_value=True):
            self.widget._on_macro(sample_outlook())
        self.assertEqual(self.widget._macro_timer.interval(), main.MACRO_REFRESH_SECONDS * 1000)

    def test_key_release_notifies_only_when_enabled(self):
        self.widget._on_macro(sample_outlook(new_release=True))
        title, body = self.widget._notifier.send.call_args[0][1:]
        self.assertEqual(self.widget._notifier.send.call_args[0][0], "macro")
        self.assertIn("非农", title)
        self.assertIn("利空黄金", body)
        self.widget._notifier.send.reset_mock()
        self.widget.cfg["macro_notify"] = False
        self.widget._on_macro(sample_outlook(new_release=True))
        self.widget._notifier.send.assert_not_called()
        self.widget._on_notification_finished("macro", False)
        self.assertFalse(hasattr(self.widget, "notified_macro"))

    def test_disabling_outlook_hides_bar_and_stops_polling(self):
        self.widget.show()
        full_height = self.widget.height()
        self.widget._schedule_macro(60)
        self.widget._apply_settings(dict(DEFAULT_CONFIG, outlook_enabled=False))
        self.assertTrue(self.widget.outlook_bar.isHidden())
        self.assertFalse(self.widget._macro_timer.isActive())
        self.assertLess(self.widget.height(), full_height)
        self.widget._apply_settings(dict(DEFAULT_CONFIG))
        self.assertFalse(self.widget.outlook_bar.isHidden())
        self.assertTrue(self.widget._macro_timer.isActive())

    def test_new_api_key_starts_ai_work_without_refetching(self):
        self.widget._outlook = sample_outlook()
        self.widget._apply_settings(dict(DEFAULT_CONFIG, deepseek_api_key="sk-new"))
        self.widget._macro.plan_ai.assert_called_once_with(self.widget._outlook)
        self.widget._macro.refresh.assert_not_called()

    def test_card_morphs_into_the_panel_and_back(self):
        self.widget.show()
        self.widget._outlook = sample_outlook()
        self.widget._open_outlook()
        opening = self.widget._morph
        dialog = self.widget._outlook_dialog
        self.assertIsNotNone(opening)
        self.assertTrue(self.widget.isHidden())  # the card has become the morphing panel
        self.assertFalse(dialog.isVisible())
        self.widget._open_outlook()  # a second click mid-flight is ignored
        self.assertIs(self.widget._morph, opening)
        opening.finish_now()
        self.app.processEvents()
        self.assertTrue(dialog.isVisible())
        self.assertIsNone(self.widget._morph)

        dialog.reject()  # Esc routes through the animated close
        closing = self.widget._morph
        self.assertIsNotNone(closing)
        self.assertIsNone(self.widget._outlook_dialog)
        self.assertTrue(self.widget.isHidden())
        closing.finish_now()
        self.app.processEvents()
        self.assertFalse(self.widget.isHidden())
        self.assertIsNone(self.widget._morph)

    def test_panel_header_mirrors_the_live_quote_while_the_card_is_hidden(self):
        self.widget._on_price(self.quote(price=1025.38, change_pct=1.25))
        self.widget._open_outlook()
        header = self.widget._outlook_dialog.quote_label.text()
        self.assertIn("1,025.38", header)
        self.assertIn("+1.25%", header)
        self.widget._on_price({"ok": False, "status": "closed", "error": "closed"})
        self.assertIn("休市", self.widget._outlook_dialog.quote_label.text())

    def test_panel_opens_from_the_card_corner_facing_the_screen_centre(self):
        geo = self.widget.docker.screen_geometry()
        self.widget.move(geo.right() - self.widget.width() - 20, geo.top() + 40)
        card = self.widget.frameGeometry()
        rect = self.widget._panel_rect(QSize(452, 700), True)
        self.assertEqual((rect.right(), rect.top()), (card.right(), card.top()))
        self.widget.move(geo.left() + 10, geo.bottom() - self.widget.height() - 10)
        card = self.widget.frameGeometry()
        rect = self.widget._panel_rect(QSize(452, 700), True)
        self.assertEqual((rect.left(), rect.bottom()), (card.left(), card.bottom()))
        self.assertEqual(self.widget._panel_rect(QSize(452, 700), False).center(), geo.center())

    def test_hidden_card_pops_the_panel_in_and_quit_cancels_the_morph(self):
        self.widget.hide()
        self.widget._open_outlook()
        self.assertIsNone(self.widget._morph._small_shot)
        with patch.object(QApplication, "quit"):
            self.widget._request_quit()
        self.assertIsNone(self.widget._morph)
        self.assertIsNone(self.widget._outlook_dialog)

    def test_outlook_dialog_renders_every_state(self):
        dialog = OutlookDialog()
        self.addCleanup(dialog.close)
        dialog.show()
        dialog.set_outlook(None, loading=True)
        self.assertFalse(dialog.refresh_button.isEnabled())
        report = sample_outlook()
        dialog.set_outlook(report, ai_ready=True)
        self.assertIn(report.stance, dialog.stance_label.text())
        self.assertIn("1 个数据源异常", dialog.source_label.text())
        self.assertTrue(dialog.deep_button.isEnabled())
        dialog.set_outlook(report, loading=True, ai_ready=True, ai_busy=True)
        self.assertIn("刷新中", dialog.meta_label.text())
        self.assertFalse(dialog.deep_button.isEnabled())
        dialog.set_outlook(report, ai_ready=True, deep_wait="冷却中 42s")
        self.assertFalse(dialog.deep_button.isEnabled())
        self.assertEqual(dialog.deep_button.text(), "冷却中 42s")
        self.assertFalse(dialog.grab().isNull())

    def test_manual_deep_read_reveals_fresh_result(self):
        dialog = OutlookDialog()
        self.addCleanup(dialog.close)
        dialog.show()
        report = sample_outlook()
        dialog.set_outlook(report, ai_ready=True)
        requested = []
        dialog.deep_read_requested.connect(lambda: requested.append(True))
        dialog.deep_button.click()
        self.assertEqual(requested, [True])
        dialog.set_outlook(report, ai_ready=True, ai_busy=True)
        self.assertEqual(dialog.deep_button.text(), "解读中…")
        fresh = replace(report, deep=DeepRead(at=report.generated_at + 5, stance="偏空", summary="加息预期压制",
                                              drivers=[], risks=[], trigger="手动请求", model="m"))
        with patch.object(QTimer, "singleShot") as single_shot:
            dialog.set_outlook(fresh, ai_ready=True, deep_wait="冷却中 60s")
        self.assertEqual(single_shot.call_args_list[0].args[1], dialog._reveal_ai)
        self.assertIsNone(dialog._awaiting_deep)


class PriceFetcherTest(unittest.TestCase):
    def test_unexpected_fetch_exception_becomes_error_result(self):
        fetcher = main.PriceFetcher()
        results = []
        fetcher.result_ready.connect(results.append)
        with patch.object(main, "fetch_gold_price_result", side_effect=RuntimeError("broken payload")):
            fetcher.run()
        self.assertFalse(results[0]["ok"])
        self.assertIn("broken payload", results[0]["error"])


if __name__ == "__main__":
    unittest.main()

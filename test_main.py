import os
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QEvent, QPoint
from PyQt6.QtGui import QContextMenuEvent
from PyQt6.QtWidgets import QApplication, QMenu

import main
from settings import DEFAULT_CONFIG


class GoldWidgetTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setQuitOnLastWindowClosed(False)

    def setUp(self):
        for patcher in (
            patch.object(main, "load_config", return_value=dict(DEFAULT_CONFIG)),
            patch.object(main, "append_log"),
            patch.object(main, "NotificationDispatcher"),
            patch.object(main.GoldWidget, "_init_tray", lambda widget: setattr(widget, "tray", MagicMock())),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        with patch.object(main.GoldWidget, "_fetch_price"):
            self.widget = main.GoldWidget()
        for timer in (self.widget.timer, self.widget._source_transition_timer, self.widget._status_timer):
            timer.stop()

    def tearDown(self):
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
        self.widget.show()
        self.assertFalse(self.widget._dock_hover_timer.isActive())
        self.widget._dock_edge = "right"
        self.widget._dock_geo = self.widget._current_screen_geometry()
        self.widget._set_dock_collapsed(True, animate=False)
        self.assertTrue(self.widget._dock_hover_timer.isActive())
        self.widget.hide()
        self.assertFalse(self.widget._dock_hover_timer.isActive())

    def test_chart_has_dedicated_space_and_handles_empty_flat_and_gap_data(self):
        self.widget.show()
        self.app.processEvents()
        self.assertGreater(self.widget.chart.y(), self.widget.range_label.geometry().bottom())
        self.assertLess(self.widget.chart.geometry().bottom(), self.widget.status_label.y())
        for entries in ([], [(1, 800), (2, 800)], [(1, 800), (250, 810), (300, 805)]):
            self.widget.chart.set_series(entries, 300, main.QColor("#d7bc7d"), 30, now=300)
            self.assertFalse(self.widget.grab().isNull())


class PriceFetcherTest(unittest.TestCase):
    def test_unexpected_fetch_exception_becomes_error_result(self):
        fetcher = main.PriceFetcher()
        results = []
        fetcher.price_fetched.connect(results.append)
        with patch.object(main, "fetch_gold_price_result", side_effect=RuntimeError("broken payload")):
            fetcher.run()
        self.assertFalse(results[0]["ok"])
        self.assertIn("broken payload", results[0]["error"])


if __name__ == "__main__":
    unittest.main()

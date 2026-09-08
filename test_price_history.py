import unittest
from unittest.mock import patch

from price_history import PriceHistory


class PriceHistoryTest(unittest.TestCase):
    def test_coarse_sampling_does_not_mislabel_a_five_minute_move_as_one_minute(self):
        history = PriceHistory()
        history.add("cmb", 800, now=0)
        history.add("cmb", 880, now=300)
        self.assertIsNone(history.interval_change("cmb", 880, 60, 300, now=300))

    def test_full_two_hour_window_survives_five_second_sampling(self):
        history = PriceHistory()
        for elapsed in range(0, 7201, 5):
            price = 100.0 + elapsed / 720
            self.assertTrue(history.add("cmb", price, quote_timestamp=100000 + elapsed, now=elapsed))

        samples = history.window("cmb", 7200, now=7200)
        self.assertEqual(len(samples), 1441)
        self.assertEqual(samples[0], (0, 100.0))
        self.assertEqual(samples[-1], (7200, 110.0))
        self.assertAlmostEqual(history.interval_change("cmb", 110, 7200, 5, now=7200), 10.0)

    def test_each_source_keeps_its_own_baseline_and_quote_watermark(self):
        history = PriceHistory()
        self.assertTrue(history.add("cmb", 100, quote_timestamp=1000, now=0))
        self.assertTrue(history.add("intl", 200, quote_timestamp=1000, now=0))
        history.add("cmb", 110, quote_timestamp=1060, now=60)
        history.add("intl", 210, quote_timestamp=1060, now=60)

        self.assertEqual(history.window("cmb", 60, now=60), [(0, 100), (60, 110)])
        self.assertEqual(history.window("intl", 60, now=60), [(0, 200), (60, 210)])
        self.assertAlmostEqual(history.interval_change("cmb", 110, 60, 5, now=60), 10.0)
        self.assertAlmostEqual(history.interval_change("intl", 210, 60, 5, now=60), 5.0)
        self.assertIsNone(history.interval_change("new_source", 210, 60, 5, now=60))

    def test_repeated_and_older_exchange_quotes_cannot_create_new_observations(self):
        history = PriceHistory()
        self.assertTrue(history.add("cmb", 100, quote_timestamp=1000, now=0))
        self.assertFalse(history.add("cmb", 100, quote_timestamp=1000, now=5))
        self.assertFalse(history.add("cmb", 800, quote_timestamp=999, now=10))
        self.assertEqual(history.window("cmb", 60, now=10), [(0, 100)])

        self.assertTrue(history.add("cmb", 110, quote_timestamp=1001, now=15))
        self.assertEqual(history.window("cmb", 60, now=15), [(0, 100), (15, 110)])

    def test_composite_price_can_change_while_older_fx_timestamp_stays_fixed(self):
        history = PriceHistory()
        history.add("intl", 200, quote_timestamp=1000, now=0)
        self.assertTrue(history.add("intl", 201, quote_timestamp=1000, now=5))
        self.assertFalse(history.add("intl", 201, quote_timestamp=1000, now=10))
        self.assertFalse(history.add("intl", 202, quote_timestamp=999, now=15))
        # A newer exchange observation remains useful even with no price move.
        self.assertTrue(history.add("intl", 201, quote_timestamp=1001, now=20))
        self.assertEqual(history.window("intl", 60, now=20), [(0, 200), (5, 201), (20, 201)])

    def test_rejected_local_time_does_not_advance_quote_watermark(self):
        for rejected_now in (99, 100):
            with self.subTest(rejected_now=rejected_now):
                history = PriceHistory()
                history.add("cmb", 100, quote_timestamp=1000, now=100)
                self.assertFalse(history.add("cmb", 900, quote_timestamp=2000, now=rejected_now))
                self.assertTrue(history.add("cmb", 110, quote_timestamp=1500, now=101))
                self.assertEqual(history.window("cmb", 60, now=101), [(100, 100), (101, 110)])

    def test_retention_covers_reference_tolerance_then_expires_idle_sources(self):
        history = PriceHistory()
        history.add("cmb", 100, quote_timestamp=1000, now=0)
        history.add("intl", 200, quote_timestamp=1000, now=0)
        # A five-minute refresh allows a reference up to ten minutes before
        # the two-hour target. Preserve that oldest supported reference.
        self.assertAlmostEqual(history.interval_change("cmb", 110, 7200, 300, now=7800), 10.0)
        self.assertEqual(history.window("intl", 10000, now=7800), [(0, 200)])

        self.assertIsNone(history.interval_change("cmb", 110, 7200, 300, now=7801))
        self.assertEqual(history.window("intl", 10000, now=7801), [])

    def test_sparse_history_does_not_treat_a_stale_reference_as_an_interval_move(self):
        history = PriceHistory()
        history.add("cmb", 100, quote_timestamp=1000, now=0)
        history.add("cmb", 110, quote_timestamp=1072, now=72)

        # Allow modest sampling jitter, but never a second full minute.
        self.assertAlmostEqual(history.interval_change("cmb", 110, 60, 5, now=72), 10.0)
        self.assertIsNone(history.interval_change("cmb", 110, 60, 5, now=73))
        self.assertIsNone(history.interval_change("cmb", 110, 60, 5, now=120))

    def test_interval_uses_latest_observation_at_or_before_target(self):
        history = PriceHistory()
        history.add("cmb", 80, now=0)
        history.add("cmb", 100, now=30)
        history.add("cmb", 200, now=40)
        # At t=95 the minute-ago target is t=35: t=40 is too recent.
        self.assertAlmostEqual(history.interval_change("cmb", 110, 60, 5, now=95), 10.0)

    def test_default_clock_is_monotonic_even_if_wall_clock_is_unavailable(self):
        history = PriceHistory()
        with patch("price_history.time.monotonic", side_effect=[100, 160, 160, 160]), \
                patch("price_history.time.time", side_effect=AssertionError("wall clock used")):
            self.assertTrue(history.add("cmb", 100))
            self.assertTrue(history.add("cmb", 110))
            self.assertEqual(history.window("cmb", 60), [(100, 100), (160, 110)])
            self.assertAlmostEqual(history.interval_change("cmb", 110, 60, 5), 10.0)

    def test_invalid_price_does_not_consume_exchange_timestamp(self):
        for invalid in (float("nan"), float("inf"), float("-inf"), 0, -1):
            with self.subTest(invalid=invalid):
                history = PriceHistory()
                self.assertFalse(history.add("cmb", invalid, quote_timestamp=1000, now=0))
                self.assertTrue(history.add("cmb", 100, quote_timestamp=1000, now=1))
                self.assertEqual(history.window("cmb", 60, now=1), [(1, 100)])


if __name__ == "__main__":
    unittest.main()

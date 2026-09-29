import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QRect, QRectF
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication

import morph


class SpringTest(unittest.TestCase):
    def test_opening_spring_overshoots_once_and_settles(self):
        samples = [morph.spring(i / 200, 0.68) for i in range(201)]
        self.assertEqual((samples[0], samples[-1]), (0.0, 1.0))
        peak = max(samples)
        self.assertGreater(peak, 1.03)
        self.assertLess(peak, 1.08)
        rising = samples[: samples.index(peak) + 1]
        self.assertEqual(rising, sorted(rising))
        self.assertLess(abs(morph.spring(0.9, 0.68) - 1.0), 0.01)

    def test_closing_spring_barely_overshoots(self):
        self.assertLess(max(morph.spring(i / 200, 0.9) for i in range(201)), 1.003)

    def test_smoothstep_clamps(self):
        self.assertEqual(morph.smoothstep(0.2, 0.4, 0.1), 0.0)
        self.assertEqual(morph.smoothstep(0.2, 0.4, 0.5), 1.0)
        self.assertAlmostEqual(morph.smoothstep(0.2, 0.4, 0.3), 0.5)


class CardMorphTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.small = QRect(600, 40, 188, 202)
        self.big = QRect(336, 40, 452, 700)
        self.shot_small = QPixmap(188, 202)
        self.shot_big = QPixmap(452, 700)

    def make(self, expanding=True, small_shot=True):
        card = morph.CardMorph(self.small, self.big, self.shot_small if small_shot else None, self.shot_big, expanding)
        self.addCleanup(card.close)
        return card

    def test_rect_runs_from_card_to_panel_and_back(self):
        opening = self.make(expanding=True)
        self.assertEqual(opening.card_rect(0.0), QRectF(self.small))
        self.assertEqual(opening.card_rect(1.0), QRectF(self.big))
        closing = self.make(expanding=False)
        self.assertEqual(closing.card_rect(0.0), QRectF(self.big))
        self.assertEqual(closing.card_rect(1.0), QRectF(self.small))

    def test_shared_corner_stays_put_and_overshoot_stays_inside_the_overlay(self):
        opening = self.make()
        frame = QRectF(opening.geometry())
        for step in range(0, 101, 5):
            rect = opening.card_rect(step / 100)
            self.assertAlmostEqual(rect.right(), QRectF(self.small).right(), places=6)
            self.assertAlmostEqual(rect.top(), self.small.top(), places=6)
            self.assertTrue(frame.contains(rect), step)
        widest = max(opening.card_rect(step / 100).width() for step in range(101))
        self.assertGreater(widest, self.big.width())

    def test_painting_every_phase_and_the_pop_in_variant(self):
        for card in (self.make(), self.make(expanding=False), self.make(small_shot=False)):
            for step in (0.0, 0.1, 0.3, 0.6, 1.0):
                card.set_progress(step)
                self.assertFalse(card.grab().isNull())

    def test_finish_now_emits_once(self):
        card = self.make()
        done = []
        card.finished.connect(lambda: done.append(True))
        card.start()
        card.finish_now()
        self.app.processEvents()
        self.assertEqual(done, [True])


if __name__ == "__main__":
    unittest.main()

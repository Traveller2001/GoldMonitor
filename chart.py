"""Small trend chart with its own layout space and visible data gaps."""

import time

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget


class Sparkline(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(32)
        self.setAccessibleName("当前数据源价格走势")
        self._history = []
        self._seconds = 300
        self._now = time.monotonic()
        self._gap = 90
        self._color = QColor("#d7bc7d")

    def set_series(self, history, seconds, color, refresh_seconds, now=None):
        self._history = history
        self._seconds = seconds
        self._now = time.monotonic() if now is None else now
        self._gap = max(90, refresh_seconds * 3)
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        left, right, top, bottom = 3, self.width() - 4, 5, self.height() - 6
        painter.setPen(QPen(QColor(255, 255, 255, 16), 1, Qt.PenStyle.DashLine))
        for fraction in (0.0, 0.5, 1.0):
            y = top + (bottom - top) * fraction
            painter.drawLine(QPointF(left, y), QPointF(right, y))

        if len(self._history) < 2:
            painter.setPen(QColor("#8e929c"))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "正在积累走势数据")
            painter.end()
            return

        prices = [price for _, price in self._history]
        center = (min(prices) + max(prices)) / 2
        span = max(max(prices) - min(prices), 0.1) * 1.2
        p_min = center - span / 2
        segments = [[]]
        last_ts = None
        for ts, price in self._history:
            if last_ts is not None and ts - last_ts > self._gap:
                segments.append([])
            x = left + max(0, min(1, (ts - (self._now - self._seconds)) / self._seconds)) * (right - left)
            y = bottom - (price - p_min) / span * (bottom - top)
            segments[-1].append(QPointF(x, y))
            last_ts = ts

        gradient = QLinearGradient(0, top, 0, bottom)
        fill_color = QColor(self._color)
        fill_color.setAlpha(42)
        gradient.setColorAt(0, fill_color)
        fill_color.setAlpha(0)
        gradient.setColorAt(1, fill_color)
        for points in segments:
            line = QPainterPath(points[0])
            for point in points[1:]:
                line.lineTo(point)
            area = QPainterPath(line)
            area.lineTo(points[-1].x(), bottom)
            area.lineTo(points[0].x(), bottom)
            area.closeSubpath()
            painter.fillPath(area, gradient)
            painter.setPen(QPen(self._color, 1.7))
            painter.drawPath(line)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawEllipse(segments[-1][-1], 2.8, 2.8)
        painter.end()

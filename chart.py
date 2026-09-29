"""Small trend chart with its own layout space and visible data gaps."""

import time

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QWidget

import theme


class Sparkline(QWidget):
    """Price path for the selected window; a dashed line marks where it began."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(30)
        self.setAccessibleName("当前数据源价格走势")
        self._history = []
        self._seconds = 300
        self._now = time.monotonic()
        self._gap = 90
        self._color = QColor(theme.GOLD)

    def set_color(self, color):
        self._color = QColor(color)
        self.update()

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
        left, right, top, bottom = 3.0, self.width() - 5.0, 4.0, self.height() - 4.0

        if len(self._history) < 2:
            painter.setPen(QPen(QColor(255, 255, 255, 22), 1, Qt.PenStyle.DashLine))
            painter.drawLine(QPointF(left, (top + bottom) / 2), QPointF(right, (top + bottom) / 2))
            painter.setFont(theme.ui_font(7.5))
            painter.setPen(QColor(theme.TEXT_MUTED))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "正在积累走势数据")
            painter.end()
            return

        prices = [price for _, price in self._history]
        center = (min(prices) + max(prices)) / 2
        span = max(max(prices) - min(prices), 0.1) * 1.2
        low = center - span / 2

        def y_of(price):
            return bottom - (price - low) / span * (bottom - top)

        reference = y_of(prices[0])
        painter.setPen(QPen(QColor(255, 255, 255, 30), 1, Qt.PenStyle.DashLine))
        painter.drawLine(QPointF(left, reference), QPointF(right, reference))

        segments = [[]]
        last_ts = None
        for ts, price in self._history:
            if last_ts is not None and ts - last_ts > self._gap:
                segments.append([])
            ratio = max(0.0, min(1.0, (ts - (self._now - self._seconds)) / self._seconds))
            segments[-1].append(QPointF(left + ratio * (right - left), y_of(price)))
            last_ts = ts

        fill = QLinearGradient(0, top, 0, bottom)
        fill.setColorAt(0, theme.with_alpha(self._color, 48))
        fill.setColorAt(1, theme.with_alpha(self._color, 0))
        line_pen = QPen(self._color, 1.6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        for points in segments:
            line = QPainterPath(points[0])
            for point in points[1:]:
                line.lineTo(point)
            area = QPainterPath(line)
            area.lineTo(points[-1].x(), bottom)
            area.lineTo(points[0].x(), bottom)
            area.closeSubpath()
            painter.fillPath(area, fill)
            painter.setPen(line_pen)
            painter.drawPath(line)

        tip = segments[-1][-1]
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.with_alpha(self._color, 60))
        painter.drawEllipse(tip, 4.6, 4.6)
        painter.setBrush(self._color)
        painter.drawEllipse(tip, 2.4, 2.4)
        painter.end()

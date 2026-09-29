"""Small painted widgets shared by the floating panel and the dialogs.

Icons and badges are drawn with QPainter so they look identical on macOS,
Windows and Linux instead of depending on each platform's glyph fonts.
"""

import math
from typing import Optional

from PyQt6.QtCore import QPointF, QRectF, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen
from PyQt6.QtWidgets import QAbstractButton, QSizePolicy, QWidget

import theme


class IconButton(QAbstractButton):
    """Flat square button with a vector icon: refresh, settings, close, chevron."""

    def __init__(self, icon, tooltip, parent=None, size=22):
        # type: (str, str, Optional[QWidget], int) -> None
        super().__init__(parent)
        self._icon = icon
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip)
        self.setFixedSize(size, size)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def sizeHint(self):
        return QSize(self.width(), self.height())

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        hovered = self.underMouse() and self.isEnabled()
        if hovered or self.isDown():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 34 if self.isDown() else 22))
            painter.drawRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 6, 6)
        if not self.isEnabled():
            color = QColor(theme.TEXT_FAINT)
        else:
            color = QColor(theme.GOLD if hovered else theme.TEXT_DIM)
        pen = QPen(color, 1.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.translate((self.width() - 16) / 2, (self.height() - 16) / 2)
        getattr(self, f"_draw_{self._icon}", self._draw_chevron)(painter)
        painter.end()

    @staticmethod
    def _draw_refresh(painter):
        center, radius = QPointF(8, 8), 5.2
        rect = QRectF(center.x() - radius, center.y() - radius, radius * 2, radius * 2)
        path = QPainterPath()
        path.arcMoveTo(rect, -30)
        path.arcTo(rect, -30, -300)  # clockwise from lower-right round to upper-right
        painter.drawPath(path)
        angle = math.radians(30)
        tip = QPointF(center.x() + radius * math.cos(angle), center.y() - radius * math.sin(angle))
        direction = (math.sin(angle), math.cos(angle))  # clockwise tangent
        for spread in (0.6, -0.6):
            dx = direction[0] * math.cos(spread) - direction[1] * math.sin(spread)
            dy = direction[0] * math.sin(spread) + direction[1] * math.cos(spread)
            painter.drawLine(tip, QPointF(tip.x() - dx * 3.4, tip.y() - dy * 3.4))

    @staticmethod
    def _draw_settings(painter):
        for y, knob in ((4.0, 10.5), (8.0, 5.5), (12.0, 9.0)):
            painter.drawLine(QPointF(2.5, y), QPointF(knob - 2.1, y))
            painter.drawLine(QPointF(knob + 2.1, y), QPointF(13.5, y))
            painter.drawEllipse(QPointF(knob, y), 1.7, 1.7)

    @staticmethod
    def _draw_close(painter):
        painter.drawLine(QPointF(4.5, 4.5), QPointF(11.5, 11.5))
        painter.drawLine(QPointF(11.5, 4.5), QPointF(4.5, 11.5))

    @staticmethod
    def _draw_chevron(painter):
        painter.drawPolyline([QPointF(6.5, 4), QPointF(10.5, 8), QPointF(6.5, 12)])


class CoinBadge(QWidget):
    """Gold coin with an "Au" mark, matching the tray icon."""

    def __init__(self, parent=None, diameter=19):
        super().__init__(parent)
        self.setFixedSize(diameter, diameter)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_coin(painter, QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))
        painter.end()


def paint_coin(painter, rect):
    # type: (QPainter, QRectF) -> None
    gradient = QLinearGradient(rect.topLeft(), rect.bottomRight())
    gradient.setColorAt(0.0, QColor("#f7dea0"))
    gradient.setColorAt(0.55, QColor("#dcb160"))
    gradient.setColorAt(1.0, QColor("#a9782c"))
    painter.setPen(QPen(QColor(255, 236, 190, 150), max(0.8, rect.width() / 24)))
    painter.setBrush(gradient)
    painter.drawEllipse(rect)
    font = QFont(theme.ui_font(1, QFont.Weight.Bold))
    font.setPixelSize(max(6, int(rect.height() * 0.46)))
    painter.setFont(font)
    painter.setPen(QColor("#5b3c0e"))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "Au")


class OutlookBar(QWidget):
    """One-line summary of the macro outlook; click to open the details."""

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(22)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAccessibleName("宏观展望")
        self._stance = ""
        self._score = ""
        self._detail = "正在获取宏观数据…"
        self._tone = 0
        self._dim = True
        self._pressed = False

    def set_message(self, text):
        # type: (str) -> None
        self._stance, self._score, self._detail, self._tone, self._dim = "", "", text, 0, True
        self.setAccessibleDescription(text)
        self.update()

    def set_outlook(self, stance, score, detail, tone):
        # type: (str, Optional[int], str, int) -> None
        self._stance = stance
        self._score = "" if score is None else f"{score:+d}".replace("-", "−")
        self._detail = detail
        self._tone = tone
        self._dim = False
        self.setAccessibleDescription(f"{stance} {self._score} {detail}")
        self.update()

    def text(self):
        # type: () -> str
        return " ".join(part for part in (self._stance, self._score, self._detail) if part)

    def _accent(self):
        # type: () -> QColor
        if self._dim:
            return QColor(theme.TEXT_MUTED)
        return QColor(theme.UP if self._tone > 0 else theme.DOWN if self._tone < 0 else theme.GOLD)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed = True
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._pressed:
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pressed and event.button() == Qt.MouseButton.LeftButton:
            self._pressed = False
            if self.rect().contains(event.position().toPoint()):
                self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        accent = self._accent()
        hovered = self.underMouse()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(theme.with_alpha(accent, 70 if hovered else 44), 1))
        painter.setBrush(theme.with_alpha(accent, 34 if hovered else 20))
        painter.drawRoundedRect(rect, 7, 7)

        x = 8.0
        baseline_font = theme.ui_font(8)
        painter.setFont(baseline_font)
        painter.setPen(QColor(theme.TEXT_MUTED))
        metrics = QFontMetrics(baseline_font)
        label = "展望"
        painter.drawText(QRectF(x, 0, 30, self.height()), Qt.AlignmentFlag.AlignVCenter, label)
        x += metrics.horizontalAdvance(label) + 6

        if self._stance:
            glyph = "▲" if self._tone > 0 else "▼" if self._tone < 0 else "◆"
            stance_font = theme.ui_font(8.5, QFont.Weight.DemiBold)
            painter.setFont(stance_font)
            painter.setPen(accent)
            text = f"{glyph}{self._stance}"
            painter.drawText(QRectF(x, 0, 80, self.height()), Qt.AlignmentFlag.AlignVCenter, text)
            x += QFontMetrics(stance_font).horizontalAdvance(text) + 4
        if self._score:
            score_font = theme.mono_font(8.5, QFont.Weight.Bold)
            painter.setFont(score_font)
            painter.setPen(accent)
            painter.drawText(QRectF(x, 0, 40, self.height()), Qt.AlignmentFlag.AlignVCenter, self._score)
            x += QFontMetrics(score_font).horizontalAdvance(self._score) + 6

        chevron_left = self.width() - 15
        painter.setFont(baseline_font)
        painter.setPen(QColor(theme.TEXT_DIM if not self._dim else theme.TEXT_MUTED))
        available = int(chevron_left - x - 2)
        if available > 12 and self._detail:
            detail = metrics.elidedText(self._detail, Qt.TextElideMode.ElideRight, available)
            painter.drawText(QRectF(x, 0, available, self.height()),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, detail)

        pen = QPen(QColor(theme.GOLD if hovered else theme.TEXT_MUTED), 1.3,
                   Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        mid = self.height() / 2
        painter.drawPolyline([QPointF(chevron_left + 3, mid - 3.5), QPointF(chevron_left + 6.5, mid),
                              QPointF(chevron_left + 3, mid + 3.5)])
        painter.end()


class StatusDot(QWidget):
    """Colored status dot with a soft halo."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(8, 8)
        self._color = QColor(theme.TEXT_MUTED)

    def set_color(self, color):
        # type: (str) -> None
        self._color = QColor(color)
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.with_alpha(self._color, 60))
        painter.drawEllipse(QRectF(0, 0, 8, 8))
        painter.setBrush(self._color)
        painter.drawEllipse(QRectF(2, 2, 4, 4))
        painter.end()

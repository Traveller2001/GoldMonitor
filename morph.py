"""Spring-driven morph between the floating card and a larger panel.

A short-lived overlay window paints snapshots of both windows inside one
rounded card whose rect follows a damped spring. No real window is resized
frame by frame (that stutters on macOS); the real windows swap in at rest.
"""

import math
from typing import Optional

from PyQt6.QtCore import QEasingCurve, QRect, QRectF, Qt, QTimer, QVariantAnimation, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import QWidget

import theme

RADIUS = 16.0


def spring(t, damping):
    # type: (float, float) -> float
    """Unit step response of a damped spring, time-scaled so it settles at t = 1.

    damping < 1 overshoots once: 0.68 peaks about 5% past the target, 0.9 barely.
    """
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    omega = 6.0 / damping  # ζω = 6, so the residual at t = 1 is e^-6 ≈ 0.25%
    damped = omega * math.sqrt(1.0 - damping * damping)
    envelope = math.exp(-damping * omega * t)
    return 1.0 - envelope * (math.cos(damped * t) + damping * omega / damped * math.sin(damped * t))


def smoothstep(edge0, edge1, x):
    # type: (float, float, float) -> float
    if x <= edge0:
        return 0.0
    if x >= edge1:
        return 1.0
    x = (x - edge0) / (edge1 - edge0)
    return x * x * (3.0 - 2.0 * x)


def lerp_rect(a, b, s):
    # type: (QRectF, QRectF, float) -> QRectF
    return QRectF(
        a.x() + (b.x() - a.x()) * s, a.y() + (b.y() - a.y()) * s,
        a.width() + (b.width() - a.width()) * s, a.height() + (b.height() - a.height()) * s,
    )


def snapshot(widget):
    # type: (QWidget) -> QPixmap
    """Render a widget that may not have been shown yet, with its layout settled."""
    widget.ensurePolished()
    if widget.layout() is not None:
        widget.layout().activate()
    return widget.grab()


def _anchor(small, big):
    # type: (QRectF, QRectF) -> tuple
    """Which side of the card the content hugs: the side the small card sits on."""
    def side(small_center, big_center):
        if small_center > big_center + 1:
            return 1.0
        if small_center < big_center - 1:
            return 0.0
        return 0.5
    return side(small.center().x(), big.center().x()), side(small.center().y(), big.center().y())


class CardMorph(QWidget):
    """Animate between ``small`` and ``big`` screen rects, in either direction.

    ``small_shot`` may be None (no source card): the panel then pops in from a
    slightly smaller rect instead of growing out of the card.
    """

    finished = pyqtSignal()

    # (duration ms, damping ratio). Opening bounces once; closing settles softly.
    EXPAND = (560, 0.68)
    COLLAPSE = (380, 0.9)

    def __init__(self, small, big, small_shot, big_shot, expanding):
        # type: (QRect, QRect, Optional[QPixmap], QPixmap, bool) -> None
        super().__init__(None, Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                         | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
        for attribute in (Qt.WidgetAttribute.WA_TranslucentBackground,
                          Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                          Qt.WidgetAttribute.WA_ShowWithoutActivating,
                          Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow,
                          Qt.WidgetAttribute.WA_DeleteOnClose):
            self.setAttribute(attribute, True)
        self._small = QRectF(small)
        self._big = QRectF(big)
        self._small_shot = small_shot
        self._big_shot = big_shot
        self._expanding = expanding
        self._anchor = _anchor(self._small, self._big)
        duration, self._damping = self.EXPAND if expanding else self.COLLAPSE
        travel = max(abs(self._big.width() - self._small.width()), abs(self._big.height() - self._small.height()),
                     abs(self._big.x() - self._small.x()), abs(self._big.y() - self._small.y()))
        pad = 30 + 0.12 * travel  # room for the overshoot and the shadow
        self._frame = self._small.united(self._big).adjusted(-pad, -pad, pad, pad).toAlignedRect()
        self.setGeometry(self._frame)
        self._t = 0.0
        self._animation = QVariantAnimation(self)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.setDuration(duration)
        self._animation.setEasingCurve(QEasingCurve.Type.Linear)  # the spring shapes the motion
        self._animation.valueChanged.connect(self.set_progress)
        self._animation.finished.connect(self._on_finished)

    # ------------------------------------------------------------- control
    def start(self):
        self.show()
        self.raise_()
        self.repaint()  # paint synchronously so the swap with the real window is seamless
        self._animation.start()

    def set_progress(self, t):
        # type: (float) -> None
        self._t = max(0.0, min(1.0, float(t)))
        self.update()

    def finish_now(self):
        """Jump to the end (used when quitting or in tests)."""
        if self._animation.state() == QVariantAnimation.State.Running:
            self._animation.setCurrentTime(self._animation.duration())
        else:
            self.set_progress(1.0)
            self._on_finished()

    def dismiss(self):
        """Hide one frame later, after the real window has painted over us."""
        QTimer.singleShot(24, self.close)

    def _on_finished(self):
        self.set_progress(1.0)
        self.finished.emit()

    # --------------------------------------------------------------- state
    def openness(self, t=None):
        # type: (Optional[float]) -> float
        """0 = small card, 1 = full panel; exceeds 1 briefly while overshooting."""
        s = spring(self._t if t is None else t, self._damping)
        return s if self._expanding else 1.0 - s

    def card_rect(self, t=None):
        # type: (Optional[float]) -> QRectF
        """Current card rect in global coordinates."""
        return lerp_rect(self._small, self._big, self.openness(t))

    # ------------------------------------------------------------ painting
    def paintEvent(self, event):
        t = self._t
        openness = self.openness()
        rect = self.card_rect().translated(-self._frame.x(), -self._frame.y())
        big_alpha = smoothstep(0.08, 0.5, openness)
        if self._small_shot is None:
            small_alpha = 0.0
            big_alpha = smoothstep(0.0, 0.55, t) if self._expanding else 1.0 - smoothstep(0.1, 0.9, t)
        else:
            small_alpha = 1.0 - smoothstep(0.0, 0.3, openness)
        # The opaque card body carries the cross-fade, so the two half-transparent
        # snapshots never let the desktop wash through. It gives way only where one
        # snapshot alone matches a real window: the small card at rest, the panel at rest.
        small_cover = 1.0 - smoothstep(0.0, 0.06, openness)
        body_alpha = (1.0 - smoothstep(0.55, 0.95, openness)) * (1.0 - small_alpha * small_cover)
        if self._small_shot is None:
            body_alpha = 0.0  # a pop-in is just the panel scaling and fading as a whole

        painter = QPainter(self)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        path = QPainterPath()
        path.addRoundedRect(rect, RADIUS, RADIUS)

        lift = math.sin(math.pi * t)  # the card rises mid-flight and settles at rest
        if lift > 0.01:
            painter.setPen(Qt.PenStyle.NoPen)
            for layer in range(1, 7):
                spread = 2.2 * layer
                painter.setBrush(QColor(0, 0, 0, int(15 * lift * (1 - layer / 7.5))))
                painter.drawRoundedRect(rect.adjusted(-spread, -spread + 5, spread, spread + 7),
                                        RADIUS + spread, RADIUS + spread)

        painter.save()
        painter.setClipPath(path)
        if body_alpha > 0.001:
            painter.setOpacity(body_alpha)
            painter.fillPath(path, theme.PANEL)
        self._draw(painter, self._big_shot, rect, big_alpha, cover=True)
        self._draw(painter, self._small_shot, rect, small_alpha, cover=False)
        painter.restore()

        if body_alpha > 0.001:
            painter.setOpacity(body_alpha)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(theme.BORDER, 1))
            painter.drawPath(path)
        painter.end()

    def _draw(self, painter, shot, rect, alpha, cover):
        # type: (QPainter, Optional[QPixmap], QRectF, float, bool) -> None
        if shot is None or shot.isNull() or alpha <= 0.001:
            return
        ratio = shot.devicePixelRatio() or 1.0
        width, height = shot.width() / ratio, shot.height() / ratio
        if cover:
            # The panel fills the card and is revealed as it grows.
            scale = max(rect.width() / width, rect.height() / height)
        else:
            # The small card keeps its proportions and only swells a little.
            scale = min(1.12, max(0.9, min(rect.width() / width, rect.height() / height)))
        target = QRectF(0, 0, width * scale, height * scale)
        target.moveLeft(rect.left() + self._anchor[0] * (rect.width() - target.width()))
        target.moveTop(rect.top() + self._anchor[1] * (rect.height() - target.height()))
        painter.setOpacity(alpha)
        painter.drawPixmap(target, shot, QRectF(0, 0, shot.width(), shot.height()))

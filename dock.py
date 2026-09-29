"""Edge docking for a frameless window: snap to a screen edge, slide out on hover."""

from typing import Optional

from PyQt6.QtCore import QEasingCurve, QObject, QPoint, QPropertyAnimation, QRect, QTimer
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import QApplication, QWidget


def _clamp(value, low, high):
    # type: (int, int, int) -> int
    if low > high:
        return low
    return max(low, min(value, high))


class EdgeDocker(QObject):
    """Owns the docking state machine so the window only forwards its events.

    Released near an edge, the window collapses to a ``peek``-pixel strip.
    Hovering the strip (or a padded hot zone around it) slides it back out;
    leaving hides it again after ``hide_delay_ms``.
    """

    def __init__(self, window, peek=24, snap_distance=68, hide_delay_ms=420,
                 hotzone_thickness=44, hotzone_padding=56):
        # type: (QWidget, int, int, int, int, int) -> None
        super().__init__(window)
        self._window = window
        self.peek = peek
        self.snap_distance = snap_distance
        self.hide_delay_ms = hide_delay_ms
        self.hotzone_thickness = hotzone_thickness
        self.hotzone_padding = hotzone_padding
        self.edge = None  # type: Optional[str]
        self.geo = None  # type: Optional[QRect]
        self.collapsed = False
        self.dragging = False

        self.animation = QPropertyAnimation(window, b"pos", self)
        self.animation.setDuration(180)
        self.animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self.hide_timer = QTimer(self)
        self.hide_timer.setSingleShot(True)
        self.hide_timer.timeout.connect(self._collapse)
        self.hover_timer = QTimer(self)
        self.hover_timer.setInterval(90)
        self.hover_timer.timeout.connect(self._check_hotzone)

    @property
    def docked(self):
        # type: () -> bool
        return self.edge is not None

    # ------------------------------------------------------------ geometry
    def screen_geometry(self, global_point=None):
        # type: (Optional[QPoint]) -> QRect
        window = self._window
        screen = QApplication.screenAt(global_point) if global_point is not None else None
        screen = screen or QApplication.screenAt(window.frameGeometry().center())
        screen = screen or window.screen() or QApplication.primaryScreen()
        if screen is None:
            return QRect(window.pos(), window.size())
        return screen.availableGeometry()

    def clamp_to_screen(self, pos, geo):
        # type: (QPoint, QRect) -> QPoint
        window = self._window
        return QPoint(
            _clamp(pos.x(), geo.x(), geo.x() + max(0, geo.width() - window.width())),
            _clamp(pos.y(), geo.y(), geo.y() + max(0, geo.height() - window.height())),
        )

    def detect_edge(self, geo, global_point=None):
        # type: (QRect, Optional[QPoint]) -> Optional[str]
        frame = self._window.frameGeometry()
        right_edge = geo.x() + geo.width()
        bottom_edge = geo.y() + geo.height()
        distances = {
            "left": abs(frame.x() - geo.x()),
            "right": abs(right_edge - (frame.x() + frame.width())),
            "top": abs(frame.y() - geo.y()),
            "bottom": abs(bottom_edge - (frame.y() + frame.height())),
        }
        if global_point is not None:
            distances["left"] = min(distances["left"], abs(global_point.x() - geo.x()))
            distances["right"] = min(distances["right"], abs(right_edge - global_point.x()))
            distances["top"] = min(distances["top"], abs(global_point.y() - geo.y()))
            distances["bottom"] = min(distances["bottom"], abs(bottom_edge - global_point.y()))
        edge, distance = min(distances.items(), key=lambda item: item[1])
        return edge if distance <= self.snap_distance else None

    def target_pos(self, edge, collapsed):
        # type: (str, bool) -> QPoint
        window = self._window
        geo = self.geo or self.screen_geometry()
        width, height = window.width(), window.height()
        max_x = geo.x() + max(0, geo.width() - width)
        max_y = geo.y() + max(0, geo.height() - height)
        if edge == "left":
            return QPoint(geo.x() - width + self.peek if collapsed else geo.x(),
                          _clamp(window.y(), geo.y(), max_y))
        if edge == "right":
            right = geo.x() + geo.width()
            return QPoint(right - self.peek if collapsed else right - width,
                          _clamp(window.y(), geo.y(), max_y))
        if edge == "top":
            return QPoint(_clamp(window.x(), geo.x(), max_x),
                          geo.y() - height + self.peek if collapsed else geo.y())
        bottom = geo.y() + geo.height()
        return QPoint(_clamp(window.x(), geo.x(), max_x),
                      bottom - self.peek if collapsed else bottom - height)

    def hotzone_rect(self):
        # type: () -> QRect
        if not self.docked or self.geo is None:
            return QRect()
        geo = self.geo
        frame = self._window.frameGeometry()
        pad, thickness = self.hotzone_padding, self.hotzone_thickness
        right_edge = geo.x() + geo.width()
        bottom_edge = geo.y() + geo.height()
        if self.edge in ("left", "right"):
            top = max(geo.y(), frame.y() - pad)
            bottom = min(bottom_edge, frame.y() + frame.height() + pad)
            x = geo.x() if self.edge == "left" else right_edge - thickness
            return QRect(x, top, thickness, max(1, bottom - top))
        left = max(geo.x(), frame.x() - pad)
        right = min(right_edge, frame.x() + frame.width() + pad)
        y = geo.y() if self.edge == "top" else bottom_edge - thickness
        return QRect(left, y, max(1, right - left), thickness)

    # --------------------------------------------------------------- state
    def set_collapsed(self, collapsed, animate=True):
        # type: (bool, bool) -> None
        if not self.docked:
            return
        self.collapsed = collapsed
        if self._window.isVisible():
            self.hover_timer.start()
        target = self.target_pos(self.edge, collapsed)
        self.animation.stop()
        if animate and target != self._window.pos():
            self.animation.setStartValue(self._window.pos())
            self.animation.setEndValue(target)
            self.animation.start()
        else:
            self._window.move(target)

    def dock(self, edge, geo):
        # type: (str, QRect) -> None
        self.edge = edge
        self.geo = geo
        self.set_collapsed(True, animate=True)

    def undock(self):
        self.stop()
        self.edge = None
        self.geo = None
        self.collapsed = False

    def stop(self):
        self.hide_timer.stop()
        self.hover_timer.stop()
        self.animation.stop()

    def cursor_inside(self):
        # type: () -> bool
        window = self._window
        return window.isVisible() and window.rect().contains(window.mapFromGlobal(QCursor.pos()))

    def schedule_hide(self):
        if self.docked and not self.dragging:
            self.hide_timer.start(self.hide_delay_ms)

    def _collapse(self):
        if (not self.docked or self.dragging or self.cursor_inside()
                or QApplication.activePopupWidget() is not None):
            return
        self.set_collapsed(True, animate=True)

    def _check_hotzone(self):
        if not self.docked or self.dragging or QApplication.activePopupWidget() is not None:
            return
        in_hotzone = self._window.isVisible() and self.hotzone_rect().contains(QCursor.pos())
        if self.collapsed:
            if in_hotzone:
                self.hide_timer.stop()
                self.set_collapsed(False, animate=True)
            return
        if self.cursor_inside() or in_hotzone:
            self.hide_timer.stop()
        elif not self.hide_timer.isActive():
            self.schedule_hide()

    # ------------------------------------------------- window event hooks
    def press(self):
        """Left button pressed on the window: expand immediately so it can be dragged."""
        self.hide_timer.stop()
        self.animation.stop()
        self.dragging = True
        if self.docked and self.collapsed:
            self.set_collapsed(False, animate=False)

    def drag_started(self):
        if self.docked:
            self.undock()

    def release(self, global_point, moved):
        # type: (QPoint, bool) -> None
        self.dragging = False
        if moved:
            geo = self.screen_geometry(global_point)
            edge = self.detect_edge(geo, global_point)
            if edge:
                self.dock(edge, geo)
            else:
                self._window.move(self.clamp_to_screen(self._window.pos(), geo))
                self.undock()
        elif self.docked and not self.cursor_inside():
            self.schedule_hide()

    def enter(self):
        self.hide_timer.stop()
        if self.docked and self.collapsed:
            self.set_collapsed(False, animate=True)

    def leave(self):
        self.schedule_hide()

    def shown(self):
        if self.docked:
            self.hover_timer.start()

    def hidden(self):
        self.hover_timer.stop()
        self.hide_timer.stop()

    def reveal(self):
        """Expand without animation, e.g. before showing a menu or from the tray."""
        self.hide_timer.stop()
        if self.docked and self.collapsed:
            self.set_collapsed(False, animate=False)

    def popup_closed(self):
        if self.docked and not self.cursor_inside():
            self.schedule_hide()

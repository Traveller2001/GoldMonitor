import html
import platform
import subprocess
import sys
import time
from collections import deque
from typing import Optional

from PyQt6.QtCore import QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRect, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QCursor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

from api import fetch_gold_price_result
from logs import LogsDialog, append_log
from settings import SettingsDialog, load_config


class ClickableLabel(QLabel):
    clicked = pyqtSignal()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
        else:
            super().mousePressEvent(event)


class PriceFetcher(QThread):
    price_fetched = pyqtSignal(object)

    def __init__(self, force_source="auto"):
        super().__init__()
        self._force_source = force_source

    def run(self):
        self.price_fetched.emit(fetch_gold_price_result(self._force_source))


def _clamp(value, low, high):
    # type: (int, int, int) -> int
    if low > high:
        return low
    return max(low, min(value, high))


def _blend_color(start, end, ratio):
    # type: (QColor, QColor, float) -> QColor
    ratio = max(0.0, min(1.0, ratio))
    return QColor(
        round(start.red() + (end.red() - start.red()) * ratio),
        round(start.green() + (end.green() - start.green()) * ratio),
        round(start.blue() + (end.blue() - start.blue()) * ratio),
        round(start.alpha() + (end.alpha() - start.alpha()) * ratio),
    )


def _css_rgba(color):
    # type: (QColor) -> str
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {color.alpha()})"


class GoldWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.last_price = None  # type: Optional[float]
        self._current_source = None  # type: Optional[str]
        self._force_source = "auto"  # type: str  # "auto", "cmb", "intl"
        self.notified_high = False
        self.notified_low = False
        self._drag_pos = None  # type: Optional[QPoint]
        self._fetcher = None  # type: Optional[PriceFetcher]
        self._settings_dialog = None  # type: Optional[SettingsDialog]
        self._logs_dialog = None  # type: Optional[LogsDialog]
        self._price_history = deque(maxlen=1000)  # (timestamp, price, source)
        self._interval_change_pct = None  # type: Optional[float]
        self._movement_theme = self._build_movement_theme(None)
        self._dock_edge = None  # type: Optional[str]
        self._dock_geo = None  # type: Optional[QRect]
        self._dock_collapsed = False
        self._drag_has_moved = False
        self._peek_size = 24
        self._snap_distance = 68
        self._dock_hide_delay_ms = 420
        self._dock_hotzone_thickness = 44
        self._dock_hotzone_padding = 56
        self._dock_animation = QPropertyAnimation(self, b"pos", self)
        self._dock_animation.setDuration(180)
        self._dock_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._dock_hide_timer = QTimer(self)
        self._dock_hide_timer.setSingleShot(True)
        self._dock_hide_timer.timeout.connect(self._collapse_dock)
        self._dock_hover_timer = QTimer(self)
        self._dock_hover_timer.setInterval(90)
        self._dock_hover_timer.timeout.connect(self._check_dock_hotzone)

        self._init_ui()
        self._init_tray()
        self._init_timer()
        self._dock_hover_timer.start()
        append_log("INFO", "app_start", "程序启动")
        self._fetch_price()

    def _init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self.setFixedSize(200, 190)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 10)
        layout.setSpacing(1)

        self.title_label = ClickableLabel("Au(T+D)")
        self.title_label.setFont(QFont("PingFang SC", 10))
        self.title_label.setStyleSheet("color: rgba(255,255,255,0.5);")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label.setCursor(Qt.CursorShape.PointingHandCursor)
        self.title_label.clicked.connect(self._toggle_source)
        layout.addWidget(self.title_label)

        self.price_label = QLabel("--")
        self.price_label.setFont(QFont("Menlo", 26, QFont.Weight.Bold))
        self.price_label.setStyleSheet("color: white;")
        self.price_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.price_label)

        # 日涨跌
        self.daily_label = QLabel("")
        self.daily_label.setFont(QFont("Menlo", 10))
        self.daily_label.setStyleSheet("color: rgba(255,255,255,0.5);")
        self.daily_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.daily_label)

        # 区间涨跌
        self.interval_label = QLabel("")
        self.interval_label.setFont(QFont("Menlo", 10))
        self.interval_label.setStyleSheet("color: rgba(255,255,255,0.5);")
        self.interval_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.interval_label)

        self.range_label = QLabel("")
        self.range_label.setFont(QFont("PingFang SC", 9))
        self.range_label.setStyleSheet("color: rgba(255,255,255,0.35);")
        self.range_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.range_label)

        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.x() + geo.width() - 220, geo.y() + 40)

    def _init_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setToolTip("金价监控")

        pixmap = QPixmap(32, 32)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(255, 200, 50))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(2, 2, 28, 28)
        painter.setPen(QColor(180, 130, 0))
        painter.setFont(QFont("Arial", 16, QFont.Weight.Bold))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "Au")
        painter.end()
        self.tray.setIcon(QIcon(pixmap))

        menu = QMenu(self)
        action_show = QAction("显示", self)
        action_show.triggered.connect(self._show_widget)
        menu.addAction(action_show)

        action_hide = QAction("隐藏", self)
        action_hide.triggered.connect(self.hide)
        menu.addAction(action_hide)

        menu.addSeparator()

        action_logs = QAction("日志", self)
        action_logs.triggered.connect(self._schedule_open_logs)
        menu.addAction(action_logs)

        action_settings = QAction("设置", self)
        action_settings.triggered.connect(self._schedule_open_settings)
        menu.addAction(action_settings)

        action_refresh = QAction("刷新", self)
        action_refresh.triggered.connect(self._fetch_price)
        menu.addAction(action_refresh)

        menu.addSeparator()

        action_quit = QAction("退出", self)
        action_quit.triggered.connect(QApplication.quit)
        menu.addAction(action_quit)

        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.show()

    def _init_timer(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._fetch_price)
        self.timer.start(self.cfg["refresh_interval"] * 1000)

    def _fetch_price(self):
        if self._fetcher and self._fetcher.isRunning():
            return
        self._fetcher = PriceFetcher(self._force_source)
        self._fetcher.price_fetched.connect(self._on_price)
        self._fetcher.start()

    def _current_screen_geometry(self, global_point=None):
        # type: (Optional[QPoint]) -> QRect
        screen = QApplication.screenAt(global_point) if global_point is not None else None
        if screen is None:
            screen = QApplication.screenAt(self.frameGeometry().center())
        if screen is None:
            screen = self.screen()
        if screen is None:
            screen = QApplication.primaryScreen()
        if screen is None:
            return QRect(self.pos(), self.size())
        return screen.availableGeometry()

    def _clamp_pos_to_screen(self, pos, geo):
        # type: (QPoint, QRect) -> QPoint
        max_x = geo.x() + max(0, geo.width() - self.width())
        max_y = geo.y() + max(0, geo.height() - self.height())
        return QPoint(
            _clamp(pos.x(), geo.x(), max_x),
            _clamp(pos.y(), geo.y(), max_y),
        )

    def _detect_snap_edge(self, geo, global_point=None):
        # type: (QRect, Optional[QPoint]) -> Optional[str]
        frame = self.frameGeometry()
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
        return edge if distance <= self._snap_distance else None

    def _dock_target_pos(self, edge, collapsed):
        # type: (str, bool) -> QPoint
        geo = self._dock_geo or self._current_screen_geometry()
        max_x = geo.x() + max(0, geo.width() - self.width())
        max_y = geo.y() + max(0, geo.height() - self.height())

        if edge == "left":
            x = geo.x() - self.width() + self._peek_size if collapsed else geo.x()
            y = _clamp(self.y(), geo.y(), max_y)
            return QPoint(x, y)
        if edge == "right":
            x = geo.x() + geo.width() - self._peek_size if collapsed else geo.x() + geo.width() - self.width()
            y = _clamp(self.y(), geo.y(), max_y)
            return QPoint(x, y)
        if edge == "top":
            x = _clamp(self.x(), geo.x(), max_x)
            y = geo.y() - self.height() + self._peek_size if collapsed else geo.y()
            return QPoint(x, y)

        x = _clamp(self.x(), geo.x(), max_x)
        y = geo.y() + geo.height() - self._peek_size if collapsed else geo.y() + geo.height() - self.height()
        return QPoint(x, y)

    def _animate_to(self, target):
        # type: (QPoint) -> None
        self._dock_animation.stop()
        if target == self.pos():
            self.move(target)
            return
        self._dock_animation.setStartValue(self.pos())
        self._dock_animation.setEndValue(target)
        self._dock_animation.start()

    def _set_dock_collapsed(self, collapsed, animate=True):
        # type: (bool, bool) -> None
        if not self._dock_edge:
            return

        self._dock_collapsed = collapsed
        target = self._dock_target_pos(self._dock_edge, collapsed)
        if animate:
            self._animate_to(target)
        else:
            self._dock_animation.stop()
            self.move(target)

    def _clear_dock_state(self):
        self._dock_hide_timer.stop()
        self._dock_animation.stop()
        self._dock_edge = None
        self._dock_geo = None
        self._dock_collapsed = False

    def _is_cursor_inside(self):
        # type: () -> bool
        if not self.isVisible():
            return False
        return self.rect().contains(self.mapFromGlobal(QCursor.pos()))

    def _schedule_dock_hide(self):
        if self._dock_edge and self._drag_pos is None:
            self._dock_hide_timer.start(self._dock_hide_delay_ms)

    def _collapse_dock(self):
        if not self._dock_edge or self._drag_pos is not None or self._is_cursor_inside():
            return
        self._set_dock_collapsed(True, animate=True)

    def _dock_hotzone_rect(self):
        # type: () -> QRect
        if not self._dock_edge or self._dock_geo is None:
            return QRect()

        geo = self._dock_geo
        frame = self.frameGeometry()
        pad = self._dock_hotzone_padding
        thickness = self._dock_hotzone_thickness
        right_edge = geo.x() + geo.width()
        bottom_edge = geo.y() + geo.height()

        if self._dock_edge in ("left", "right"):
            top = max(geo.y(), frame.y() - pad)
            bottom = min(bottom_edge, frame.y() + frame.height() + pad)
            height = max(1, bottom - top)
            x = geo.x() if self._dock_edge == "left" else right_edge - thickness
            return QRect(x, top, thickness, height)

        left = max(geo.x(), frame.x() - pad)
        right = min(right_edge, frame.x() + frame.width() + pad)
        width = max(1, right - left)
        y = geo.y() if self._dock_edge == "top" else bottom_edge - thickness
        return QRect(left, y, width, thickness)

    def _is_cursor_in_dock_hotzone(self):
        # type: () -> bool
        if not self.isVisible():
            return False
        return self._dock_hotzone_rect().contains(QCursor.pos())

    def _check_dock_hotzone(self):
        # type: () -> None
        if not self._dock_edge or self._drag_pos is not None:
            return

        cursor_in_hotzone = self._is_cursor_in_dock_hotzone()
        cursor_inside = self._is_cursor_inside()

        if self._dock_collapsed:
            if cursor_in_hotzone:
                self._dock_hide_timer.stop()
                self._set_dock_collapsed(False, animate=True)
            return

        if cursor_inside or cursor_in_hotzone:
            self._dock_hide_timer.stop()
        elif not self._dock_hide_timer.isActive():
            self._schedule_dock_hide()

    def _build_movement_theme(self, interval_pct):
        # type: (Optional[float]) -> dict
        neutral = {
            "price": QColor(255, 255, 255),
            "interval": QColor(255, 255, 255, 128),
            "sparkline": QColor(255, 255, 255, 128),
            "background": QColor(255, 255, 255, 0),
            "border": QColor(255, 255, 255, 30),
        }
        if interval_pct is None:
            return neutral

        threshold = max(float(self.cfg.get("color_threshold", 0.5)), 0.01)
        magnitude = abs(interval_pct)
        if magnitude < threshold:
            softness = magnitude / threshold
            neutral["sparkline"] = QColor(255, 255, 255, 128 + round(softness * 24))
            neutral["background"] = QColor(255, 255, 255, round(softness * 10))
            neutral["border"] = QColor(255, 255, 255, 30 + round(softness * 8))
            return neutral

        ratio = min((magnitude - threshold) / (threshold * 2), 1.0)
        if interval_pct > 0:
            base = QColor(255, 150, 95)
            peak = QColor(255, 68, 68)
        else:
            base = QColor(102, 225, 155)
            peak = QColor(54, 210, 110)

        accent = _blend_color(base, peak, ratio)
        return {
            "price": accent,
            "interval": accent,
            "sparkline": QColor(accent.red(), accent.green(), accent.blue(), 160 + round(ratio * 60)),
            "background": QColor(accent.red(), accent.green(), accent.blue(), 28 + round(ratio * 56)),
            "border": QColor(accent.red(), accent.green(), accent.blue(), 48 + round(ratio * 56)),
        }

    def _apply_movement_theme(self, interval_pct):
        # type: (Optional[float]) -> None
        self._interval_change_pct = interval_pct
        self._movement_theme = self._build_movement_theme(interval_pct)
        self.price_label.setStyleSheet(f"color: {self._movement_theme['price'].name()};")
        self.interval_label.setStyleSheet(f"color: {_css_rgba(self._movement_theme['interval'])};")
        self.update()

    def _toggle_source(self):
        if self._force_source == "intl" or (self._force_source == "auto" and self._current_source == "intl"):
            self._set_source("cmb")
        else:
            self._set_source("intl")

    def _set_source(self, source):
        # type: (str) -> None
        if self._force_source == source:
            return
        self._force_source = source
        if source == "cmb":
            self.title_label.setText("Au(T+D)")
        elif source == "intl":
            self.title_label.setText("XAU 国际金价")
        append_log("INFO", "source_manual", f"手动切换数据源: {source}")
        self._fetch_price()
        QTimer.singleShot(2000, self._fetch_price)

    def _on_price(self, result):
        if not isinstance(result, dict) or not result.get("ok"):
            error = "unknown error"
            if isinstance(result, dict):
                error = result.get("error", error)
            append_log("ERROR", "fetch_failed", f"抓取失败: {error}")
            return

        data = result["data"]
        price = data["price"]
        source = data.get("source", "cmb")
        self.last_price = price
        now = time.time()

        if self._current_source != source:
            prev_source = self._current_source
            self._current_source = source
            if prev_source is not None:
                append_log("INFO", "source_switched", f"数据源切换 {prev_source} -> {source}")

        self._price_history.append((now, price, source))

        # 标题：区分数据源
        if source == "intl":
            self.title_label.setText("XAU 国际金价")
        else:
            self.title_label.setText("Au(T+D)")

        self.price_label.setText(f"¥{price:.2f}")

        # 日涨跌（对比昨收）
        change = data["change"]
        change_pct = data["change_pct"]
        if change != 0 or change_pct != 0:
            sign = "+" if change >= 0 else ""
            arrow = "▲" if change >= 0 else "▼"
            self.daily_label.setText(f"日 {arrow}{sign}{change_pct:.2f}%")

            if change_pct > 0:
                daily_color = "#ff4444"
            elif change_pct < 0:
                daily_color = "#44ff44"
            else:
                daily_color = "rgba(255,255,255,0.5)"
            self.daily_label.setStyleSheet(f"color: {daily_color};")
        else:
            self.daily_label.setText("日 --")
            self.daily_label.setStyleSheet("color: rgba(255,255,255,0.3);")

        # 区间涨跌（对比 N 分钟前）
        interval_min = self.cfg.get("interval_minutes", 5)
        cutoff = now - interval_min * 60
        ref_price = None
        ref_ts = 0.0
        for ts, p, hist_source in self._history_for_source(source):
            if hist_source != source:
                continue
            if ts <= cutoff:
                ref_price = p
                ref_ts = ts
            else:
                break

        # 防止使用过期参考价（如切换回一个很久没更新的数据源）
        if ref_price is not None and ref_price > 0 and (now - ref_ts) < interval_min * 60 * 3:
            iv_pct = (price - ref_price) / ref_price * 100
            iv_sign = "+" if iv_pct >= 0 else ""
            iv_arrow = "▲" if iv_pct >= 0 else "▼"
            self.interval_label.setText(f"{interval_min}min {iv_arrow}{iv_sign}{iv_pct:.2f}%")
            self._apply_movement_theme(iv_pct)
        else:
            self.interval_label.setText(f"{interval_min}min --")
            self._apply_movement_theme(None)

        # 高低区间（国际源无此数据）
        if source == "cmb" and data["high"] > 0:
            self.range_label.setText(f"低 {data['low']:.2f}  高 {data['high']:.2f}")
        else:
            self.range_label.setText("")
        append_log(
            "INFO",
            "fetch_success",
            f"抓取成功 source={source} price={price:.2f}",
        )
        self._check_notify(price)
        self.update()  # 触发重绘曲线

    def _history_for_source(self, source):
        return [entry for entry in self._price_history if entry[2] == source]

    def _check_notify(self, price):
        high = self.cfg["notify_high"]
        low = self.cfg["notify_low"]

        if high > 0 and price >= high and not self.notified_high:
            self.notified_high = True
            title = "金价突破高位"
            body = f"¥{price:.2f}/g 已达到 >= ¥{high:.2f}"
            if self._send_notification(title, body):
                append_log("INFO", "notify_high", f"高价阈值触发 price={price:.2f} target={high:.2f}")
            else:
                append_log("WARN", "notify_high_failed", f"高价阈值触发但通知发送失败 price={price:.2f} target={high:.2f}")
        elif high > 0 and price < high:
            self.notified_high = False

        if low > 0 and price <= low and not self.notified_low:
            self.notified_low = True
            title = "金价跌破低位"
            body = f"¥{price:.2f}/g 已达到 <= ¥{low:.2f}"
            if self._send_notification(title, body):
                append_log("INFO", "notify_low", f"低价阈值触发 price={price:.2f} target={low:.2f}")
            else:
                append_log("WARN", "notify_low_failed", f"低价阈值触发但通知发送失败 price={price:.2f} target={low:.2f}")
        elif low > 0 and price > low:
            self.notified_low = False

    def _send_notification(self, title, body):
        try:
            if platform.system() == "Darwin":
                result = subprocess.run(
                    [
                        "osascript",
                        "-e",
                        f'display notification "{body}" with title "{title}" sound name "Glass"',
                    ],
                    check=False,
                    capture_output=True,
                )
                return result.returncode == 0

            if platform.system() == "Linux":
                result = subprocess.run(
                    ["notify-send", title, body],
                    check=False,
                    capture_output=True,
                )
                return result.returncode == 0

            if platform.system() == "Windows":
                safe_title = html.escape(title, quote=True)
                safe_body = html.escape(body, quote=True)
                ps = (
                    "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                    "ContentType = WindowsRuntime] > $null; "
                    "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
                    "ContentType = WindowsRuntime] > $null; "
                    "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
                    f"$xml.LoadXml(\"<toast><visual><binding template='ToastGeneric'><text>{safe_title}</text>"
                    f"<text>{safe_body}</text></binding></visual></toast>\"); "
                    "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
                    "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('GoldMonitor').Show($toast)"
                )
                result = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    check=False,
                    capture_output=True,
                )
                return result.returncode == 0
        except Exception:
            return False

        return False

    def _show_widget(self):
        if self._dock_edge:
            self._dock_hide_timer.stop()
            self._set_dock_collapsed(False, animate=False)
        self.show()
        self.raise_()
        self.activateWindow()

    def _schedule_open_settings(self):
        QTimer.singleShot(0, self._open_settings)

    def _schedule_open_logs(self):
        QTimer.singleShot(0, self._open_logs)

    def _open_settings(self):
        if self._settings_dialog is not None:
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return

        dlg = SettingsDialog()
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.settings_changed.connect(self._apply_settings)
        dlg.finished.connect(self._on_settings_closed)
        dlg.open()
        dlg.raise_()
        dlg.activateWindow()
        self._settings_dialog = dlg

    def _open_logs(self):
        if self._logs_dialog is not None:
            self._logs_dialog.refresh_logs()
            self._logs_dialog.raise_()
            self._logs_dialog.activateWindow()
            return

        dlg = LogsDialog()
        dlg.finished.connect(self._on_logs_closed)
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        self._logs_dialog = dlg

    def _on_settings_closed(self, _result):
        self._settings_dialog = None

    def _on_logs_closed(self, _result):
        self._logs_dialog = None

    def _apply_settings(self, cfg):
        self.cfg = cfg
        self.timer.setInterval(cfg["refresh_interval"] * 1000)
        self.notified_high = False
        self.notified_low = False
        self._apply_movement_theme(self._interval_change_pct)
        append_log(
            "INFO",
            "settings_saved",
            (
                f"设置已保存 refresh_interval={cfg['refresh_interval']}s "
                f"color_threshold={cfg['color_threshold']:.2f}% "
                f"notify_high={cfg['notify_high']:.2f} notify_low={cfg['notify_low']:.2f}"
            ),
        )

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 16, 16)
        painter.setClipPath(path)
        painter.fillRect(self.rect(), QColor(28, 30, 34, 208))

        glow = self._movement_theme["background"]
        if glow.alpha() > 0:
            gradient = QLinearGradient(0, 0, self.width(), self.height())
            gradient.setColorAt(0.0, QColor(glow.red(), glow.green(), glow.blue(), min(255, glow.alpha() + 18)))
            gradient.setColorAt(0.65, glow)
            gradient.setColorAt(1.0, QColor(glow.red(), glow.green(), glow.blue(), 0))
            painter.fillRect(self.rect(), gradient)

        top_glow = QLinearGradient(0, 0, 0, 70)
        top_glow.setColorAt(0, QColor(255, 255, 255, 18))
        top_glow.setColorAt(1, QColor(255, 255, 255, 0))
        painter.fillRect(0, 0, self.width(), 70, top_glow)

        # 绘制价格曲线
        self._draw_sparkline(painter)

        # 画边框（必须重置 brush，否则会被曲线颜色填充）
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(self._movement_theme["border"])
        painter.drawPath(path)
        painter.end()

    def _draw_sparkline(self, painter):
        history = self._history_for_source(self._current_source)
        if len(history) < 2:
            return

        # 曲线区域：底部 55px
        chart_left = 12
        chart_right = self.width() - 12
        chart_top = self.height() - 60
        chart_bottom = self.height() - 12
        chart_w = chart_right - chart_left
        chart_h = chart_bottom - chart_top

        prices = [p for _, p, _ in history]
        p_min = min(prices)
        p_max = max(prices)
        p_range = p_max - p_min
        if p_range < 0.01:
            p_range = 1.0  # 价格几乎没变化时避免除零

        t_min = history[0][0]
        t_max = history[-1][0]
        t_range = t_max - t_min
        if t_range < 1:
            return

        # 构建曲线点
        points = []
        for ts, price, _ in history:
            x = chart_left + (ts - t_min) / t_range * chart_w
            y = chart_bottom - (price - p_min) / p_range * chart_h
            points.append(QPointF(x, y))

        line_color = self._movement_theme["sparkline"]

        # 画曲线
        line_path = QPainterPath()
        line_path.moveTo(points[0])
        for pt in points[1:]:
            line_path.lineTo(pt)

        from PyQt6.QtGui import QPen
        pen = QPen(line_color, 1.5)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(line_path)

        # 最新价格点
        from PyQt6.QtGui import QBrush
        painter.setBrush(QBrush(line_color))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(points[-1], 2.5, 2.5)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._dock_hide_timer.stop()
            self._dock_animation.stop()
            if self._dock_edge and self._dock_collapsed:
                self._set_dock_collapsed(False, animate=False)
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._drag_has_moved = False

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if self._dock_edge and not self._drag_has_moved:
                self._clear_dock_state()
            self._drag_has_moved = True
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        release_point = event.globalPosition().toPoint()
        geo = self._current_screen_geometry(release_point)
        if self._drag_has_moved:
            edge = self._detect_snap_edge(geo, release_point)
            if edge:
                self._dock_edge = edge
                self._dock_geo = geo
                self._set_dock_collapsed(True, animate=True)
            else:
                self.move(self._clamp_pos_to_screen(self.pos(), geo))
                self._clear_dock_state()
        elif self._dock_edge and not self._is_cursor_inside():
            self._schedule_dock_hide()
        self._drag_pos = None
        self._drag_has_moved = False

    def enterEvent(self, event):
        self._dock_hide_timer.stop()
        if self._dock_edge and self._dock_collapsed:
            self._set_dock_collapsed(False, animate=True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._dock_edge and self._drag_pos is None:
            self._schedule_dock_hide()
        super().leaveEvent(event)

    def contextMenuEvent(self, event):
        self._dock_hide_timer.stop()
        if self._dock_edge and self._dock_collapsed:
            self._set_dock_collapsed(False, animate=False)

        menu = QMenu(self)
        menu.setStyleSheet(
            """
            QMenu {
                background: #2b2b2b;
                color: #e0e0e0;
                border: 1px solid #555;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item:selected {
                background: #4a9eff;
                border-radius: 4px;
            }
            """
        )

        action_logs = QAction("日志", self)
        action_logs.triggered.connect(self._schedule_open_logs)
        menu.addAction(action_logs)

        action_settings = QAction("设置", self)
        action_settings.triggered.connect(self._schedule_open_settings)
        menu.addAction(action_settings)

        action_refresh = QAction("刷新", self)
        action_refresh.triggered.connect(self._fetch_price)
        menu.addAction(action_refresh)

        menu.addSeparator()

        for key, label in [("auto", "自动"), ("cmb", "招行金交所"), ("intl", "国际金价")]:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(self._force_source == key)
            action.triggered.connect(lambda checked, k=key: self._set_source(k))
            menu.addAction(action)

        menu.addSeparator()

        action_quit = QAction("退出", self)
        action_quit.triggered.connect(QApplication.quit)
        menu.addAction(action_quit)
        menu.exec(event.globalPos())
        if self._dock_edge and not self._is_cursor_inside():
            self._schedule_dock_hide()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    widget = GoldWidget()
    widget.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

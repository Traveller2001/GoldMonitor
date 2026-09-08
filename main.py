import sys
import time
from typing import Optional

from PyQt6.QtCore import QEvent, QEasingCurve, QPoint, QPropertyAnimation, QRect, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QCursor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMenu, QSystemTrayIcon, QToolButton, QVBoxLayout, QWidget

from api import fetch_gold_price_result, seconds_until_next_market_transition
from chart import Sparkline
from logs import LogsDialog, append_log
from notifications import NotificationDispatcher
from price_history import PriceHistory
from settings import SettingsDialog, load_config


class PriceFetcher(QThread):
    price_fetched = pyqtSignal(object)

    def run(self):
        try:
            result = fetch_gold_price_result()
        except Exception as exc:
            result = {"ok": False, "error": f"行情请求异常: {exc}"}
        self.price_fetched.emit(result)


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
        self._last_fallback_pair = None
        self.notified_high = False
        self.notified_low = False
        self._drag_pos = None  # type: Optional[QPoint]
        self._fetcher = None  # type: Optional[PriceFetcher]
        self._fetch_pending = False
        self._settings_dialog = None  # type: Optional[SettingsDialog]
        self._logs_dialog = None  # type: Optional[LogsDialog]
        self._price_history = PriceHistory()
        self._last_data = None
        self._quote_timestamp = None
        self._fetch_state = "loading"
        self._fetch_error = ""
        self._closing = False
        self._notification_retry_at = {"high": 0.0, "low": 0.0}
        self._notifier = NotificationDispatcher(self)
        self._notifier.finished.connect(self._on_notification_finished)
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
        self.setWindowTitle("GoldMonitor · 黄金行情")
        self.setFixedSize(188, 190)
        self.setStyleSheet("QLabel { background: transparent; color: #eef0f4; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(2)

        header = QHBoxLayout()
        header.setSpacing(5)
        badge = QLabel("Au")
        badge.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        badge.setFixedSize(21, 20)
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setStyleSheet("color: #e4c98a; background: rgba(224,193,121,0.13); border-radius: 8px;")
        header.addWidget(badge)
        self.title_label = QLabel("黄金行情")
        self.title_label.setFont(QFont("PingFang SC", 10, QFont.Weight.DemiBold))
        header.addWidget(self.title_label)
        header.addStretch()
        self.refresh_button = self._tool_button("↻", "刷新行情", self._fetch_price)
        header.addWidget(self.refresh_button)
        header.addWidget(self._tool_button("⚙", "设置", self._schedule_open_settings))
        layout.addLayout(header)

        self.source_label = QLabel("自动选择交易中的数据源")
        self.source_label.setFont(QFont("PingFang SC", 8))
        self.source_label.setStyleSheet("color: #b0a48c;")
        layout.addWidget(self.source_label)

        price_row = QHBoxLayout()
        price_row.setSpacing(2)
        currency = QLabel("¥")
        currency.setFont(QFont("Arial", 14))
        currency.setStyleSheet("color: #a8abb3;")
        price_row.addWidget(currency)

        self.price_label = QLabel("--")
        self.price_label.setFont(QFont("Menlo", 21, QFont.Weight.Bold))
        self.price_label.setAccessibleName("当前金价，人民币每克")
        price_row.addWidget(self.price_label)
        price_row.addStretch()
        unit_label = QLabel("/ 克")
        unit_label.setFont(QFont("PingFang SC", 8))
        unit_label.setStyleSheet("color: #9297a2;")
        unit_label.setToolTip("人民币 / 克")
        price_row.addWidget(unit_label)
        layout.addLayout(price_row)

        metrics = QHBoxLayout()
        metrics.setSpacing(8)
        self.daily_label = QLabel("--")
        self.interval_label = QLabel("--")
        self.interval_title = QLabel(f"{self.cfg['interval_minutes']}分")
        daily_title = QLabel("日")
        daily_title.setToolTip("较昨收涨跌幅")
        for title, value in ((daily_title, self.daily_label), (self.interval_title, self.interval_label)):
            column = QHBoxLayout()
            column.setSpacing(3)
            title.setFont(QFont("PingFang SC", 8))
            title.setStyleSheet("color: #9297a2;")
            value.setFont(QFont("Menlo", 9, QFont.Weight.DemiBold))
            column.addWidget(title)
            column.addWidget(value)
            column.addStretch()
            metrics.addLayout(column, 1)
        layout.addLayout(metrics)

        self.range_label = QLabel("日内低 / 高  --")
        self.range_label.setFont(QFont("PingFang SC", 8))
        self.range_label.setStyleSheet("color: #9297a2;")
        layout.addWidget(self.range_label)

        self.chart = Sparkline(self)
        layout.addWidget(self.chart)
        self.status_label = QLabel("● 正在连接行情…")
        self.status_label.setFont(QFont("PingFang SC", 8))
        self.status_label.setStyleSheet("color: #b8a778;")
        layout.addWidget(self.status_label)
        self._apply_movement_theme(None)

        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.x() + geo.width() - self.width() - 20, geo.y() + 40)

    def _tool_button(self, text, tooltip, callback):
        button = QToolButton(self)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setAccessibleName(tooltip)
        button.setFixedSize(20, 20)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet("""
            QToolButton { color: #a8abb3; background: transparent; border: none;
                          border-radius: 6px; font-size: 14px; }
            QToolButton:hover { color: #efd598; background: rgba(255,255,255,0.08); }
            QToolButton:disabled { color: #545963; }
        """)
        button.clicked.connect(callback)
        return button

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
        action_quit.triggered.connect(self._request_quit)
        menu.addAction(action_quit)

        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.show()

    def _init_timer(self):
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._fetch_price)
        self.timer.start(self.cfg["refresh_interval"] * 1000)

        self._source_transition_timer = QTimer(self)
        self._source_transition_timer.setSingleShot(True)
        self._source_transition_timer.timeout.connect(self._on_source_transition)
        self._schedule_source_transition()

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start(10000)

    def _fetch_price(self):
        if self._closing:
            return
        if self._fetcher is not None:
            self._fetch_pending = True
            return
        self._fetch_pending = False
        self.refresh_button.setEnabled(False)
        self._fetcher = PriceFetcher(self)
        self._fetcher.price_fetched.connect(self._on_price)
        self._fetcher.finished.connect(self._on_fetch_finished)
        self._update_status()
        self._fetcher.start()

    def _on_fetch_finished(self):
        fetcher = self._fetcher
        self._fetcher = None
        if fetcher is not None:
            fetcher.deleteLater()
        if self._closing:
            QApplication.quit()
            return
        self.refresh_button.setEnabled(True)
        self._update_status()
        if self._fetch_pending:
            self._fetch_pending = False
            QTimer.singleShot(0, self._fetch_price)

    def _request_quit(self):
        if self._closing:
            return
        self._closing = True
        self._fetch_pending = False
        for timer in (self.timer, self._source_transition_timer, self._status_timer,
                      self._dock_hover_timer, self._dock_hide_timer):
            timer.stop()
        self._dock_animation.stop()
        self._notifier.shutdown()
        self.tray.hide()
        self.hide()
        for dialog in (self._settings_dialog, self._logs_dialog):
            if dialog is not None:
                dialog.close()
        # Allow the bounded request to finish before Qt destroys its thread.
        if self._fetcher is None:
            QApplication.quit()

    def closeEvent(self, event):
        event.ignore()
        self._request_quit()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Quit:
            if not self._closing:
                self._request_quit()
                return True
            if self._fetcher is not None:
                return True
        return super().eventFilter(watched, event)

    def _schedule_source_transition(self):
        delay_seconds = seconds_until_next_market_transition()
        self._source_transition_timer.start(max(1000, int(delay_seconds * 1000) + 250))

    def _on_source_transition(self):
        self._fetch_price()
        self._schedule_source_transition()

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
        if self.isVisible():
            self._dock_hover_timer.start()
        target = self._dock_target_pos(self._dock_edge, collapsed)
        if animate:
            self._animate_to(target)
        else:
            self._dock_animation.stop()
            self.move(target)

    def _clear_dock_state(self):
        self._dock_hide_timer.stop()
        self._dock_hover_timer.stop()
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
        if (not self._dock_edge or self._drag_pos is not None or self._is_cursor_inside()
                or QApplication.activePopupWidget() is not None):
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
        if (not self._dock_edge or self._drag_pos is not None
                or QApplication.activePopupWidget() is not None):
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
            "interval": QColor(192, 196, 204),
            "sparkline": QColor(215, 188, 125),
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

    def _on_price(self, result):
        if self._closing:
            return
        if not isinstance(result, dict) or not result.get("ok"):
            error = result.get("error", "unknown error") if isinstance(result, dict) else "invalid result"
            state = "closed" if isinstance(result, dict) and result.get("status") == "closed" else "error"
            if state != self._fetch_state or error != self._fetch_error:
                append_log("INFO" if state == "closed" else "ERROR", "market_closed" if state == "closed" else "fetch_failed", str(error))
            self._fetch_state = state
            self._fetch_error = str(error)
            self._apply_movement_theme(None)
            self._update_status()
            return

        data = result["data"]
        price = data["price"]
        source = data.get("source", "cmb")
        if self._price_history.is_outdated(source, data.get("quote_timestamp")):
            self._on_price({"ok": False, "error": "数据源返回早于最近记录的报价，等待新报价"})
            return
        fallback_from = data.get("fallback_from")
        self.last_price = price
        self._last_data = data
        self._quote_timestamp = data.get("quote_timestamp", time.time())
        self._fetch_state = "live"
        self._fetch_error = ""
        now = time.monotonic()

        fallback_pair = (fallback_from, source) if fallback_from and fallback_from != source else None
        if fallback_pair != self._last_fallback_pair:
            if fallback_pair:
                append_log("WARN", "source_fallback", f"数据源 {fallback_from} 不可用，自动切换到 {source}")
            self._last_fallback_pair = fallback_pair

        if self._current_source != source:
            prev_source = self._current_source
            self._current_source = source
            if prev_source is not None:
                append_log("INFO", "source_switched", f"数据源切换 {prev_source} -> {source}")

        self._price_history.add(source, price, data.get("quote_timestamp"), now)
        self.price_label.setText(f"{price:,.2f}")
        self.source_label.setText("上海金交所 · Au(T+D)" if source == "cmb" else "国际现货 · 离岸人民币折算")
        self.source_label.setToolTip(
            "招商银行 Au(T+D) 行情，人民币/克" if source == "cmb" else
            "Swissquote XAU/USD × USD/CNH ÷ 金衡盎司克数；与国内 Au(T+D) 属于不同市场。"
        )

        change_pct = data.get("change_pct")
        if change_pct is not None:
            self.daily_label.setText(f"{change_pct:+.2f}%")
            daily_color = "#ff8e84" if change_pct > 0 else "#77d7aa" if change_pct < 0 else "#b4bac5"
            self.daily_label.setStyleSheet(f"color: {daily_color};")
            self.daily_label.setToolTip("当前数据源相对昨收盘价的涨跌幅")
        else:
            self.daily_label.setText("--")
            self.daily_label.setStyleSheet("color: #9297a2;")
            self.daily_label.setToolTip("当前数据源未提供可比较的昨收价")

        self._refresh_interval_view(now)
        if source == "cmb" and data.get("high", 0) > 0 and data.get("low", 0) > 0:
            self.range_label.setText(f"日内低 {data['low']:.2f}   高 {data['high']:.2f}")
        else:
            self.range_label.setText("日内低 / 高  --")
        append_log("INFO", "fetch_success", f"抓取成功 source={source} price={price:.2f}")
        self._check_notify(price)
        self._update_status()

    def _refresh_interval_view(self, now=None):
        now = time.monotonic() if now is None else now
        minutes = self.cfg["interval_minutes"]
        self.interval_title.setText(f"{minutes}分")
        self.interval_title.setToolTip(f"{minutes} 分钟涨跌幅")
        change = None
        if self.last_price is not None:
            change = self._price_history.interval_change(
                self._current_source, self.last_price, minutes * 60, self.cfg["refresh_interval"], now
            )
        self.interval_label.setText(f"{change:+.2f}%" if change is not None else "积累中")
        self.interval_label.setToolTip("对比同一数据源的历史报价；数据不足或中断时不计算涨跌")
        self._apply_movement_theme(change if self._fetch_state == "live" else None)
        self.chart.set_series(
            self._price_history.window(self._current_source, minutes * 60, now),
            minutes * 60, self._movement_theme["sparkline"], self.cfg["refresh_interval"], now,
        )
        self.chart.setToolTip(f"最近 {minutes} 分钟 · 同一数据源 · 长时间断档以空隙显示")

    def _update_status(self):
        if self._closing:
            return
        stamp = time.strftime("%H:%M:%S", time.localtime(self._quote_timestamp)) if self._quote_timestamp else "--:--:--"
        age = max(0, time.time() - self._quote_timestamp) if self._quote_timestamp else None
        stale = age is not None and age > 180
        if self._fetch_state == "closed":
            text = "● 休市 · 保留最近报价" if self.last_price is not None else "● 休市 · 等待开盘"
            color = "#a5a9b2"
        elif self._fetch_state == "error":
            text, color = "● 更新失败 · 等待重试", "#e9b479"
        elif stale:
            text, color = "● 报价已过期 · 等待更新", "#e9b479"
        elif self._fetch_state == "loading":
            text, color = "● 正在连接行情…", "#b8a778"
        elif self._fetcher is not None:
            text, color = "● 正在更新行情…", "#b8a778"
        else:
            text, color = f"● 实时 · 报价 {stamp}", "#99b5a7"
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {color};")
        detail = f"最近报价：{stamp}" if self._quote_timestamp else "尚无有效报价"
        self.status_label.setToolTip(detail + (f"\n{self._fetch_error}" if self._fetch_error else ""))
        self.price_label.setToolTip(detail)
        self.tray.setToolTip(f"GoldMonitor · ¥{self.last_price:.2f}/克\n{text}" if self.last_price is not None else f"GoldMonitor\n{text}")
        if self._fetch_state in ("closed", "error") or stale:
            self.price_label.setStyleSheet("color: #a0a5af;")
            self.interval_label.setText("--")
            self.interval_label.setStyleSheet("color: #9297a2;")
        else:
            self.price_label.setStyleSheet(f"color: {self._movement_theme['price'].name()};")

    def _check_notify(self, price):
        for kind, crossed in (("high", price >= self.cfg["notify_high"]), ("low", price <= self.cfg["notify_low"])):
            target = self.cfg[f"notify_{kind}"]
            if target <= 0 or not crossed:
                setattr(self, f"notified_{kind}", False)
                continue
            if getattr(self, f"notified_{kind}") or time.monotonic() < self._notification_retry_at[kind]:
                continue
            title = "金价突破高位" if kind == "high" else "金价跌破低位"
            operator = "≥" if kind == "high" else "≤"
            body = f"¥{price:.2f}/g 已达到 {operator} ¥{target:.2f}"
            if self._notifier.send(kind, title, body):
                setattr(self, f"notified_{kind}", True)
            else:
                self._notification_retry_at[kind] = time.monotonic() + 60

    def _on_notification_finished(self, kind, success):
        if self._closing:
            return
        if success:
            append_log("INFO", f"notify_{kind}", "价格阈值通知已发送")
        else:
            setattr(self, f"notified_{kind}", False)
            self._notification_retry_at[kind] = time.monotonic() + 60
            append_log("WARN", f"notify_{kind}_failed", "通知发送失败，60 秒后允许重试")

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
        self._notification_retry_at = {"high": 0.0, "low": 0.0}
        self._refresh_interval_view()
        self._update_status()
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
        painter.fillRect(self.rect(), QColor(25, 28, 34, 238))

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

        # 画边框（必须重置 brush，否则会被曲线颜色填充）
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(self._movement_theme["border"])
        painter.drawPath(path)
        painter.end()

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

    def hideEvent(self, event):
        self._dock_hover_timer.stop()
        self._dock_hide_timer.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        if self._dock_edge:
            self._dock_hover_timer.start()
        super().showEvent(event)

    def contextMenuEvent(self, event):
        self._dock_hide_timer.stop()
        if self._dock_edge and self._dock_collapsed:
            self._set_dock_collapsed(False, animate=False)

        self.tray_menu.exec(event.globalPos())
        if self._dock_edge and not self._is_cursor_inside():
            self._schedule_dock_hide()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    widget = GoldWidget()
    app.installEventFilter(widget)
    widget.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

import os
import sys
import time
from typing import Optional

from PyQt6.QtCore import QEvent, QPoint, QRect, QRectF, QTimer, Qt
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import QApplication, QHBoxLayout, QLabel, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

import theme
from api import fetch_gold_price_result, is_market_open, seconds_until_next_market_transition
from chart import Sparkline
from dock import EdgeDocker
from logs import LogsDialog, append_log
from morph import CardMorph, snapshot
from news_ai import AiConfig, NewsAnalyst, resolve_api_key
from notifications import NotificationDispatcher
from outlook import MacroService, Outlook, next_refresh_delay
from outlook_panel import OutlookDialog
from price_history import PriceHistory
from settings import SettingsDialog, load_config
from widgets import CoinBadge, IconButton, OutlookBar, StatusDot, paint_coin
from workers import BackgroundCall, TaskThread

APP_DIR = os.path.dirname(os.path.abspath(__file__))
AI_CACHE_PATH = os.path.join(APP_DIR, "ai_cache.json")
STALE_AFTER_SECONDS = 180
PANEL_WIDTH = 188
PANEL_HEIGHT = 176
OUTLOOK_ROW_HEIGHT = 26
MACRO_RETRY_SECONDS = 120
MACRO_REFRESH_SECONDS = 300
MACRO_CLOSED_SECONDS = 1800  # every gold market shut: weekends and the daily break

SOURCES = {
    "cmb": ("Au(T+D)", "上海金交所 Au(T+D) · 招商银行行情，人民币/克"),
    "intl": ("国际现货", "Swissquote XAU/USD × USD/CNH ÷ 31.1035 g；与国内 Au(T+D) 属于不同市场"),
}


class PriceFetcher(TaskThread):
    def __init__(self, parent=None):
        # Look the fetcher up at call time so tests can patch it.
        super().__init__(lambda: fetch_gold_price_result(), "行情请求异常", parent)


def _shrunk(rect, factor):
    # type: (QRect, float) -> QRect
    result = QRect(0, 0, round(rect.width() * factor), round(rect.height() * factor))
    result.moveCenter(rect.center())
    return result


def _label(text, size, color, weight=QFont.Weight.Normal, mono=False):
    label = QLabel(text)
    label.setFont(theme.mono_font(size, weight) if mono else theme.ui_font(size, weight))
    label.setStyleSheet(f"color: {color};")
    return label


class GoldWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        # Quote state
        self.last_price = None  # type: Optional[float]
        self._current_source = None  # type: Optional[str]
        self._last_fallback_pair = None
        self._last_data = None
        self._quote_timestamp = None
        self._fetch_state = "loading"
        self._fetch_error = ""
        self._price_history = PriceHistory()
        self._interval_change_pct = None  # type: Optional[float]
        self._movement_theme = theme.movement_theme(None, self.cfg["color_threshold"])
        # Workers and windows
        self._fetcher = None  # type: Optional[PriceFetcher]
        self._fetch_pending = False
        self._settings_dialog = None  # type: Optional[SettingsDialog]
        self._logs_dialog = None  # type: Optional[LogsDialog]
        self._outlook_dialog = None  # type: Optional[OutlookDialog]
        self._morph = None  # type: Optional[CardMorph]
        self._card_hidden_for_panel = False
        self._closing = False
        # Price alerts
        self.notified_high = False
        self.notified_low = False
        self._notification_retry_at = {"high": 0.0, "low": 0.0}
        self._notifier = NotificationDispatcher(self)
        self._notifier.finished.connect(self._on_notification_finished)
        # Dragging and edge docking
        self._drag_pos = None  # type: Optional[QPoint]
        self._drag_has_moved = False
        self.docker = EdgeDocker(self)
        # Macro outlook
        self._analyst = NewsAnalyst(AI_CACHE_PATH)
        self._macro = MacroService(self._analyst)
        self._outlook = None  # type: Optional[Outlook]
        self._macro_errors = ""
        self._macro_call = BackgroundCall("宏观数据异常", self)
        self._macro_call.result_ready.connect(self._on_macro)
        self._ai_call = BackgroundCall("AI 解读异常", self)
        self._ai_call.result_ready.connect(self._on_ai)
        self._ai_deep = False  # the running AI job includes a deep read
        # Ticks the panel's cooldown countdown while a manual deep read is unavailable.
        self._deep_wait_timer = QTimer(self)
        self._deep_wait_timer.setInterval(1000)
        self._deep_wait_timer.timeout.connect(self._render_outlook)
        self._configure_ai()

        self._init_ui()
        self._init_tray()
        self._init_timers()
        append_log("INFO", "app_start", "程序启动")
        self._fetch_price()
        self._schedule_macro(1.5)
        self._render_outlook()

    # ------------------------------------------------------------------ UI
    def _init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self.setWindowTitle("GoldMonitor · 黄金行情")
        self.setStyleSheet("QLabel { background: transparent; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 8)
        layout.setSpacing(3)

        header = QHBoxLayout()
        header.setSpacing(5)
        header.addWidget(CoinBadge(self))
        self.title_label = _label("黄金", 12, theme.TEXT, QFont.Weight.DemiBold)
        header.addWidget(self.title_label)
        self.source_label = QLabel("--")
        self.source_label.setFont(theme.ui_font(10, QFont.Weight.Medium))
        self.source_label.setStyleSheet(
            f"color: {theme.GOLD}; background: rgba(232,196,124,0.13); border-radius: 4px; padding: 0 4px;")
        self.source_label.setToolTip("自动选择交易中的数据源")
        header.addWidget(self.source_label)
        header.addStretch()
        self.refresh_button = IconButton("refresh", "刷新行情", self, size=20)
        self.refresh_button.clicked.connect(self._fetch_price)
        header.addWidget(self.refresh_button)
        settings_button = IconButton("settings", "设置", self, size=20)
        settings_button.clicked.connect(self._schedule_open_settings)
        header.addWidget(settings_button)
        layout.addLayout(header)

        price_row = QHBoxLayout()
        price_row.setSpacing(3)
        price_row.addWidget(_label("¥", 14, theme.TEXT_DIM), 0, Qt.AlignmentFlag.AlignBaseline)
        self.price_label = _label("--", 25, theme.TEXT, QFont.Weight.Bold, mono=True)
        self.price_label.setAccessibleName("当前金价，人民币每克")
        price_row.addWidget(self.price_label, 0, Qt.AlignmentFlag.AlignBaseline)
        price_row.addStretch()
        unit_label = _label("元/克", 10, theme.TEXT_MUTED)
        unit_label.setToolTip("人民币 / 克")
        price_row.addWidget(unit_label, 0, Qt.AlignmentFlag.AlignBaseline)
        layout.addLayout(price_row)

        metrics = QHBoxLayout()
        metrics.setSpacing(8)
        self.daily_label = _label("--", 11, theme.TEXT_MUTED, QFont.Weight.DemiBold, mono=True)
        self.interval_label = _label("--", 11, theme.TEXT_MUTED, QFont.Weight.DemiBold, mono=True)
        daily_title = _label("日", 10, theme.TEXT_MUTED)
        daily_title.setToolTip("较昨收涨跌幅")
        self.interval_title = _label(f"{self.cfg['interval_minutes']}分", 10, theme.TEXT_MUTED)
        for title, value in ((daily_title, self.daily_label), (self.interval_title, self.interval_label)):
            column = QHBoxLayout()
            column.setSpacing(4)
            column.addWidget(title)
            column.addWidget(value)
            column.addStretch()
            metrics.addLayout(column, 1)
        layout.addLayout(metrics)

        self.range_label = _label("日内低 / 高  --", 10, theme.TEXT_MUTED)
        layout.addWidget(self.range_label)

        self.chart = Sparkline(self)
        layout.addWidget(self.chart)

        self.outlook_bar = OutlookBar(self)
        self.outlook_bar.clicked.connect(self._schedule_open_outlook)
        layout.addWidget(self.outlook_bar)

        status_row = QHBoxLayout()
        status_row.setSpacing(5)
        self.status_dot = StatusDot(self)
        status_row.addWidget(self.status_dot)
        self.status_label = _label("正在连接行情…", 10, theme.TEXT_DIM)
        status_row.addWidget(self.status_label, 1)
        layout.addLayout(status_row)

        self._apply_outlook_visibility()
        self._apply_movement_theme(None)

        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(geo.x() + geo.width() - self.width() - 20, geo.y() + 40)

    def _apply_outlook_visibility(self):
        enabled = self.cfg["outlook_enabled"]
        self.outlook_bar.setVisible(enabled)
        height = PANEL_HEIGHT + (OUTLOOK_ROW_HEIGHT if enabled else 0)
        if self.height() != height or self.width() != PANEL_WIDTH:
            self.setFixedSize(PANEL_WIDTH, height)
            if self.docker.docked:
                self.docker.set_collapsed(self.docker.collapsed, animate=False)

    def _init_tray(self):
        self.tray = QSystemTrayIcon(self)
        self.tray.setToolTip("金价监控")
        pixmap = QPixmap(64, 64)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        paint_coin(painter, QRectF(4, 4, 56, 56))
        painter.end()
        self.tray.setIcon(QIcon(pixmap))

        menu = QMenu(self)
        for text, slot in (
            ("显示", self._show_widget), ("隐藏", self.hide), (None, None),
            ("宏观展望", self._schedule_open_outlook), ("日志", self._schedule_open_logs),
            ("设置", self._schedule_open_settings), ("刷新", self._fetch_price), (None, None),
            ("退出", self._request_quit),
        ):
            if text is None:
                menu.addSeparator()
                continue
            action = QAction(text, self)
            action.triggered.connect(slot)
            menu.addAction(action)
        self.tray_menu = menu
        self.tray.setContextMenu(menu)
        self.tray.show()

    def _init_timers(self):
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

        self._macro_timer = QTimer(self)
        self._macro_timer.setSingleShot(True)
        self._macro_timer.timeout.connect(self._fetch_macro)

    # -------------------------------------------------------------- quotes
    def _fetch_price(self):
        if self._closing:
            return
        if self._fetcher is not None:
            self._fetch_pending = True
            return
        self._fetch_pending = False
        self.refresh_button.setEnabled(False)
        self._fetcher = PriceFetcher(self)
        self._fetcher.result_ready.connect(self._on_price)
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

    def _schedule_source_transition(self):
        delay_seconds = seconds_until_next_market_transition()
        self._source_transition_timer.start(max(1000, int(delay_seconds * 1000) + 250))

    def _on_source_transition(self):
        self._fetch_price()
        self._schedule_source_transition()

    def _on_price(self, result):
        if self._closing:
            return
        if not isinstance(result, dict) or not result.get("ok"):
            error = result.get("error", "unknown error") if isinstance(result, dict) else "invalid result"
            state = "closed" if isinstance(result, dict) and result.get("status") == "closed" else "error"
            if state != self._fetch_state or error != self._fetch_error:
                append_log("INFO" if state == "closed" else "ERROR",
                           "market_closed" if state == "closed" else "fetch_failed", str(error))
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
        was_live = self._fetch_state == "live"
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

        source_changed = self._current_source != source
        if source_changed:
            if self._current_source is not None:
                append_log("INFO", "source_switched", f"数据源切换 {self._current_source} -> {source}")
            self._current_source = source
        if source_changed or not was_live:
            # Log transitions only; a line per refresh would bury real events.
            append_log("INFO", "fetch_success", f"行情已连接 source={source} price={price:.2f}")

        self._price_history.add(source, price, data.get("quote_timestamp"), now)
        self.price_label.setText(f"{price:,.2f}")
        name, detail = SOURCES.get(source, (source, source))
        self.source_label.setText(name)
        self.source_label.setToolTip(detail)

        change_pct = data.get("change_pct")
        if change_pct is not None:
            self.daily_label.setText(f"{change_pct:+.2f}%")
            self.daily_label.setStyleSheet(f"color: {theme.signed_color(change_pct)};")
            self.daily_label.setToolTip("当前数据源相对昨收盘价的涨跌幅")
        else:
            self.daily_label.setText("--")
            self.daily_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            self.daily_label.setToolTip("当前数据源未提供可比较的昨收价")

        self._refresh_interval_view(now)
        if source == "cmb" and data.get("high", 0) > 0 and data.get("low", 0) > 0:
            self.range_label.setText(f"日内低 {data['low']:.2f}   高 {data['high']:.2f}")
        else:
            self.range_label.setText("日内低 / 高  --")
        daily = f"，日 {change_pct:+.2f}%" if change_pct is not None else ""
        self._macro.price_context = f"{name} {price:.2f} 元/克{daily}"
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
        self._apply_movement_theme(change if self._is_live() else None)
        self.chart.set_series(
            self._price_history.window(self._current_source, minutes * 60, now),
            minutes * 60, self._movement_theme["sparkline"], self.cfg["refresh_interval"], now,
        )
        self.chart.setToolTip(f"最近 {minutes} 分钟 · 同一数据源 · 虚线为区间起点 · 长时间断档以空隙显示")

    def _is_live(self):
        # type: () -> bool
        if self._fetch_state != "live" or not self._quote_timestamp:
            return False
        return time.time() - self._quote_timestamp <= STALE_AFTER_SECONDS

    def _apply_movement_theme(self, interval_pct):
        # type: (Optional[float]) -> None
        self._interval_change_pct = interval_pct
        self._movement_theme = theme.movement_theme(interval_pct, self.cfg["color_threshold"])
        self.price_label.setStyleSheet(f"color: {self._movement_theme['price'].name()};")
        self.interval_label.setStyleSheet(f"color: {theme.css_rgba(self._movement_theme['interval'])};")
        self.chart.set_color(self._movement_theme["sparkline"])
        self.update()

    def _update_status(self):
        if self._closing:
            return
        stamp = time.strftime("%H:%M:%S", time.localtime(self._quote_timestamp)) if self._quote_timestamp else "--:--:--"
        stale = self._fetch_state == "live" and not self._is_live()
        if self._fetch_state == "closed":
            text = "休市 · 保留最近报价" if self.last_price is not None else "休市 · 等待开盘"
            color = theme.TEXT_MUTED
        elif self._fetch_state == "error":
            text, color = "更新失败 · 等待重试", theme.WARN
        elif stale:
            text, color = "报价已过期 · 等待更新", theme.WARN
        elif self._fetch_state == "loading":
            text, color = "正在连接行情…", theme.GOLD
        else:
            # Routine refreshes keep the live text; the disabled refresh button
            # already signals the request, and the line does not flicker.
            text, color = f"实时 · 报价 {stamp}", theme.LIVE
        self.status_label.setText(text)
        self.status_dot.set_color(color)
        detail = f"最近报价：{stamp}" if self._quote_timestamp else "尚无有效报价"
        self.status_label.setToolTip(detail + (f"\n{self._fetch_error}" if self._fetch_error else ""))
        self.price_label.setToolTip(detail)
        self.tray.setToolTip(
            f"GoldMonitor · ¥{self.last_price:.2f}/克\n{text}" if self.last_price is not None else f"GoldMonitor\n{text}"
        )
        if self._fetch_state in ("closed", "error") or stale:
            if self._interval_change_pct is not None:
                self._apply_movement_theme(None)
            self.price_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
            self.interval_label.setText("--")
            self.interval_label.setStyleSheet(f"color: {theme.TEXT_MUTED};")
        else:
            self.price_label.setStyleSheet(f"color: {self._movement_theme['price'].name()};")
        self._sync_panel_quote()

    def _sync_panel_quote(self):
        """Mirror the card's quote in the panel header, since the card hides behind it."""
        dlg = self._outlook_dialog
        if dlg is None:
            return
        source = self.source_label.text() if self._current_source else "金价"
        if self.last_price is None:
            dlg.set_quote(source, "--", theme.TEXT_MUTED)
        elif self._is_live():
            change = (self._last_data or {}).get("change_pct")
            dlg.set_quote(source, f"{self.last_price:,.2f}", self._movement_theme["price"].name(),
                          "" if change is None else f"{change:+.2f}%", theme.signed_color(change))
        else:
            dlg.set_quote(source, f"{self.last_price:,.2f}", theme.TEXT_MUTED, self.status_label.text().split(" · ")[0])

    # --------------------------------------------------------------- alerts
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
        if kind not in ("high", "low"):
            if not success:
                append_log("WARN", f"notify_{kind}_failed", "宏观通知发送失败")
            return
        if success:
            append_log("INFO", f"notify_{kind}", "价格阈值通知已发送")
        else:
            setattr(self, f"notified_{kind}", False)
            self._notification_retry_at[kind] = time.monotonic() + 60
            append_log("WARN", f"notify_{kind}_failed", "通知发送失败，60 秒后允许重试")

    def _notify_releases(self, releases):
        if not releases:
            return
        impact = sum(release.weight * release.impact for release in releases)
        verdict = "偏利多黄金" if impact >= 0.25 else "偏利空黄金" if impact <= -0.25 else "影响中性"
        body = "；".join(release.describe() for release in releases[:3]) + f" → {verdict}"
        append_log("INFO", "macro_release", body)
        if self.cfg["macro_notify"]:
            title = f"美国{releases[0].label}数据公布" if len(releases) == 1 else "美国经济数据公布"
            self._notifier.send("macro", title, body)

    # --------------------------------------------------------------- macro
    def _configure_ai(self):
        cfg = self.cfg
        self._analyst.configure(AiConfig(
            enabled=cfg["outlook_enabled"] and cfg["ai_enabled"],
            api_key=resolve_api_key(cfg["deepseek_api_key"]),
            model=cfg["deepseek_model"],
            daily_token_budget=cfg["ai_daily_token_budget"],
            tag_effort=cfg["ai_tag_effort"],
            deep_effort=cfg["ai_deep_effort"],
        ))

    def _schedule_macro(self, delay_seconds):
        if self._closing or not self.cfg["outlook_enabled"]:
            return
        self._macro_timer.start(max(1000, int(delay_seconds * 1000)))

    def _fetch_macro(self):
        if self._closing or not self.cfg["outlook_enabled"]:
            return
        self._macro_timer.stop()
        if self._macro_call.start(self._macro.refresh):
            self._render_outlook()

    def _on_macro(self, result):
        if self._closing:
            return
        if isinstance(result, Outlook):
            self._outlook = result
            errors = "；".join(result.errors)
            if errors != self._macro_errors:
                # Log changes only; a blocked source would otherwise repeat every refresh.
                append_log("WARN" if errors else "INFO", "macro_sources", errors[:600] or "宏观数据源已全部恢复")
                self._macro_errors = errors
            self._notify_releases(result.new_releases)
            market_open = is_market_open()
            self._start_ai(self._macro.plan_ai(result, market_open=market_open))
            delay = next_refresh_delay(result, time.time(), base=self._macro_base_delay(market_open))
        else:
            error = result.get("error", "unknown error") if isinstance(result, dict) else "invalid result"
            append_log("ERROR", "macro_failed", str(error))
            delay = MACRO_RETRY_SECONDS
        self._schedule_macro(delay)
        self._render_outlook()

    @staticmethod
    def _macro_base_delay(market_open):
        if market_open:
            return MACRO_REFRESH_SECONDS
        # Poll slowly while closed, but be back in time for the next open.
        return min(MACRO_CLOSED_SECONDS, max(MACRO_REFRESH_SECONDS, seconds_until_next_market_transition() + 5))

    def _start_ai(self, job):
        if job is None or self._closing:
            return
        if self._ai_call.start(lambda: self._macro.run_ai(job)):
            self._ai_deep = bool(job.deep_trigger)
            if job.deep_trigger:
                append_log("INFO", "ai_deep_read", f"DeepSeek 深度解读：{job.deep_trigger}")
            self._render_outlook()

    def _on_ai(self, result):
        if self._closing:
            return
        if isinstance(result, Outlook):
            self._outlook = result
        elif isinstance(result, dict):
            append_log("WARN", "ai_failed", str(result.get("error", "unknown error")))
        self._render_outlook()

    def _request_deep_read(self):
        self._start_ai(self._macro.plan_ai(self._outlook, manual=True))

    def _render_outlook(self):
        if not self.cfg["outlook_enabled"]:
            return
        outlook = self._outlook
        loading = self._macro_call.running
        if outlook is None:
            self.outlook_bar.set_message("正在获取宏观数据…" if loading or self._macro_timer.isActive() else "宏观数据暂不可用")
            self.outlook_bar.setToolTip("利率预期、经济数据与新闻的综合判断")
        elif outlook.score is None:
            self.outlook_bar.set_message("宏观数据不足 · 点击查看")
            self.outlook_bar.setToolTip("\n".join(outlook.errors) or "暂无可用因子")
        else:
            self.outlook_bar.set_outlook(outlook.stance, outlook.score, outlook.driver, outlook.tone)
            self.outlook_bar.setToolTip(
                f"宏观展望 {outlook.stance} {outlook.score:+d} · 置信度 {outlook.confidence}\n"
                f"{outlook.driver}\n点击查看因子拆解、事件日历和新闻"
            )
        if self._outlook_dialog is None:
            self._deep_wait_timer.stop()
            return
        running = self._ai_call.running
        if running:
            deep_wait = "" if self._ai_deep else "AI 忙"
        else:
            deep_wait = self._analyst.manual_deep_wait() if self._analyst.active else ""
        if deep_wait and not self._deep_wait_timer.isActive():
            self._deep_wait_timer.start()
        elif not deep_wait:
            self._deep_wait_timer.stop()
        self._outlook_dialog.set_outlook(
            outlook, loading=loading, ai_ready=self._analyst.active,
            ai_busy=running and self._ai_deep, deep_wait=deep_wait,
        )

    # ------------------------------------------------------------- windows
    def _show_widget(self):
        self.docker.reveal()
        self.show()
        self.raise_()
        self.activateWindow()

    def _schedule_open_settings(self):
        QTimer.singleShot(0, self._open_settings)

    def _schedule_open_logs(self):
        QTimer.singleShot(0, self._open_logs)

    def _schedule_open_outlook(self):
        QTimer.singleShot(0, self._open_outlook)

    @staticmethod
    def _focus(dialog):
        dialog.raise_()
        dialog.activateWindow()

    def _open_settings(self):
        if self._settings_dialog is not None:
            self._focus(self._settings_dialog)
            return
        dlg = SettingsDialog()
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        dlg.settings_changed.connect(self._apply_settings)
        dlg.finished.connect(self._on_settings_closed)
        dlg.open()
        self._focus(dlg)
        self._settings_dialog = dlg

    def _open_logs(self):
        if self._logs_dialog is not None:
            self._logs_dialog.refresh_logs()
            self._focus(self._logs_dialog)
            return
        dlg = LogsDialog()
        dlg.finished.connect(self._on_logs_closed)
        dlg.show()
        self._focus(dlg)
        self._logs_dialog = dlg

    def _open_outlook(self):
        if self._outlook_dialog is not None:
            self._focus(self._outlook_dialog)
            return
        if self._morph is not None or self._closing:
            return
        dlg = OutlookDialog()
        dlg.refresh_requested.connect(self._fetch_macro)
        dlg.deep_read_requested.connect(self._request_deep_read)
        dlg.finished.connect(self._on_outlook_closed)
        dlg.close_handler = self._collapse_outlook
        self._outlook_dialog = dlg
        self._render_outlook()
        self._sync_panel_quote()
        if self._outlook is None and self.cfg["outlook_enabled"] and not self._macro_call.running:
            self._fetch_macro()

        # The card grows into the panel; without a visible card the panel pops in.
        from_card = self.isVisible()
        target = self._panel_rect(dlg.size(), from_card)
        dlg.move(target.topLeft())
        small = self.frameGeometry() if from_card else _shrunk(target, 0.9)
        morph = CardMorph(small, target, self.grab() if from_card else None, snapshot(dlg), expanding=True)
        morph.finished.connect(self._on_outlook_expanded)
        self._morph = morph
        morph.start()
        if from_card:
            self._card_hidden_for_panel = True
            self.hide()

    def _on_outlook_expanded(self):
        morph, self._morph = self._morph, None
        dlg = self._outlook_dialog
        if dlg is not None and not self._closing:
            dlg.show()
            self._focus(dlg)
        if morph is not None:
            morph.dismiss()

    def _collapse_outlook(self):
        dlg = self._outlook_dialog
        if dlg is None:
            return
        if self._closing or self._morph is not None:
            dlg.force_close()
            return
        back_to_card = self._card_hidden_for_panel or self.isVisible()
        big = dlg.frameGeometry()
        small = self.frameGeometry() if back_to_card else _shrunk(big, 0.92)
        morph = CardMorph(small, big, self.grab() if back_to_card else None, dlg.grab(), expanding=False)
        morph.finished.connect(self._on_outlook_collapsed)
        self._morph = morph
        morph.start()
        dlg.force_close()
        if back_to_card and self.isVisible():
            # Shown again from the tray meanwhile: hide it so the panel lands in its place.
            self._card_hidden_for_panel = True
            self.hide()

    def _on_outlook_collapsed(self):
        morph, self._morph = self._morph, None
        if self._card_hidden_for_panel and not self._closing:
            self._card_hidden_for_panel = False
            self.show()
        if morph is not None:
            morph.dismiss()

    def _panel_rect(self, size, from_card):
        """Anchor the panel on the card's corner that faces the screen centre."""
        geo = self.docker.screen_geometry()
        margin = 8
        if not from_card:
            rect = QRect(0, 0, size.width(), size.height())
            rect.moveCenter(geo.center())
            return rect
        card = self.frameGeometry()
        x = card.right() - size.width() + 1 if card.center().x() >= geo.center().x() else card.left()
        y = card.bottom() - size.height() + 1 if card.center().y() >= geo.center().y() else card.top()
        x = max(geo.left() + margin, min(x, geo.right() - size.width() - margin + 1))
        y = max(geo.top() + margin, min(y, geo.bottom() - size.height() - margin + 1))
        return QRect(x, y, size.width(), size.height())

    def _on_settings_closed(self, _result):
        self._settings_dialog = None

    def _on_logs_closed(self, _result):
        self._logs_dialog = None

    def _on_outlook_closed(self, _result):
        self._outlook_dialog = None
        self._deep_wait_timer.stop()

    def _apply_settings(self, cfg):
        previous = self.cfg
        self.cfg = cfg
        self.timer.setInterval(cfg["refresh_interval"] * 1000)
        self.notified_high = False
        self.notified_low = False
        self._notification_retry_at = {"high": 0.0, "low": 0.0}
        self._configure_ai()
        self._apply_outlook_visibility()
        ai_keys = ("ai_enabled", "deepseek_api_key", "deepseek_model", "ai_tag_effort", "ai_deep_effort",
                   "ai_daily_token_budget")
        if not cfg["outlook_enabled"]:
            self._macro_timer.stop()
        elif not previous["outlook_enabled"]:
            self._schedule_macro(0)
        elif any(cfg[key] != previous[key] for key in ai_keys):
            # New key or model: read the headlines we already have, no refetch needed.
            self._start_ai(self._macro.plan_ai(self._outlook))
        self._refresh_interval_view()
        self._update_status()
        self._render_outlook()
        append_log(
            "INFO",
            "settings_saved",
            (
                f"设置已保存 refresh_interval={cfg['refresh_interval']}s "
                f"color_threshold={cfg['color_threshold']:.2f}% "
                f"notify_high={cfg['notify_high']:.2f} notify_low={cfg['notify_low']:.2f} "
                f"outlook={cfg['outlook_enabled']} ai={cfg['ai_enabled']} model={cfg['deepseek_model']} "
                f"effort={cfg['ai_tag_effort']}/{cfg['ai_deep_effort']}"
            ),
        )

    # ----------------------------------------------------------- lifecycle
    def _request_quit(self):
        if self._closing:
            return
        self._closing = True
        self._fetch_pending = False
        for timer in (self.timer, self._source_transition_timer, self._status_timer, self._macro_timer):
            timer.stop()
        self.docker.stop()
        self._notifier.shutdown()
        if self._morph is not None:
            self._morph.close()
            self._morph = None
        self.tray.hide()
        self.hide()
        if self._outlook_dialog is not None:
            self._outlook_dialog.force_close()
        for dialog in (self._settings_dialog, self._logs_dialog):
            if dialog is not None:
                dialog.close()
        # Allow the bounded quote request to finish before Qt destroys its thread.
        # Macro and model calls run on daemon threads and never block quitting.
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

    # ------------------------------------------------------------- painting
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0.5, 0.5, self.width() - 1, self.height() - 1, 16, 16)
        painter.setClipPath(path)
        painter.fillRect(self.rect(), theme.PANEL)

        glow = self._movement_theme["background"]
        if glow.alpha() > 0:
            gradient = QLinearGradient(0, 0, self.width(), self.height())
            gradient.setColorAt(0.0, theme.with_alpha(glow, glow.alpha() + 18))
            gradient.setColorAt(0.65, glow)
            gradient.setColorAt(1.0, theme.with_alpha(glow, 0))
            painter.fillRect(self.rect(), gradient)

        sheen = QLinearGradient(0, 0, 0, 72)
        sheen.setColorAt(0, QColor(255, 255, 255, 20))
        sheen.setColorAt(1, QColor(255, 255, 255, 0))
        painter.fillRect(0, 0, self.width(), 72, sheen)

        # Reset the brush before stroking, or the border fills with the chart color.
        painter.setClipping(False)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(self._movement_theme["border"])
        painter.drawPath(path)
        painter.end()

    # --------------------------------------------------------- mouse/dock
    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.docker.press()
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._drag_has_moved = False

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if not self._drag_has_moved:
                self.docker.drag_started()
            self._drag_has_moved = True
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self.docker.release(event.globalPosition().toPoint(), self._drag_has_moved)
        self._drag_pos = None
        self._drag_has_moved = False

    def enterEvent(self, event):
        self.docker.enter()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.docker.leave()
        super().leaveEvent(event)

    def hideEvent(self, event):
        self.docker.hidden()
        super().hideEvent(event)

    def showEvent(self, event):
        self.docker.shown()
        super().showEvent(event)

    def contextMenuEvent(self, event):
        self.docker.reveal()
        self.tray_menu.exec(event.globalPos())
        self.docker.popup_closed()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    widget = GoldWidget()
    app.installEventFilter(widget)
    widget.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

import html
import platform
import subprocess
import sys
import time
from collections import deque
from typing import Optional

from PyQt6.QtCore import QPoint, QPointF, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap, QPolygonF
from PyQt6.QtWidgets import QApplication, QLabel, QMenu, QSystemTrayIcon, QVBoxLayout, QWidget

from api import fetch_gold_price_result
from logs import LogsDialog, append_log
from settings import SettingsDialog, load_config


class PriceFetcher(QThread):
    price_fetched = pyqtSignal(object)

    def run(self):
        self.price_fetched.emit(fetch_gold_price_result())


class GoldWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.last_price = None  # type: Optional[float]
        self._current_source = None  # type: Optional[str]
        self.notified_high = False
        self.notified_low = False
        self._drag_pos = None  # type: Optional[QPoint]
        self._fetcher = None  # type: Optional[PriceFetcher]
        self._settings_dialog = None  # type: Optional[SettingsDialog]
        self._logs_dialog = None  # type: Optional[LogsDialog]
        self._price_history = deque(maxlen=1000)  # (timestamp, price, source)

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
        self.setFixedSize(200, 190)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 10)
        layout.setSpacing(1)

        self.title_label = QLabel("Au(T+D)")
        self.title_label.setFont(QFont("PingFang SC", 10))
        self.title_label.setStyleSheet("color: rgba(255,255,255,0.5);")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
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
            self.move(geo.width() - 220, 40)

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
        self._fetcher = PriceFetcher()
        self._fetcher.price_fetched.connect(self._on_price)
        self._fetcher.start()

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
            self._price_history.clear()
            self.interval_label.setText(f"{self.cfg.get('interval_minutes', 5)}min --")
            self.interval_label.setStyleSheet("color: rgba(255,255,255,0.5);")
            self.price_label.setStyleSheet("color: white;")
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
        for ts, p, hist_source in self._history_for_source(source):
            if hist_source != source:
                continue
            if ts <= cutoff:
                ref_price = p
            else:
                break

        if ref_price is not None and ref_price > 0:
            iv_pct = (price - ref_price) / ref_price * 100
            iv_sign = "+" if iv_pct >= 0 else ""
            iv_arrow = "▲" if iv_pct >= 0 else "▼"
            self.interval_label.setText(f"{interval_min}min {iv_arrow}{iv_sign}{iv_pct:.2f}%")

            threshold = self.cfg["color_threshold"]
            if iv_pct >= threshold:
                iv_color = "#ff4444"
            elif iv_pct <= -threshold:
                iv_color = "#44ff44"
            else:
                iv_color = "white"

            # 价格主色由区间涨跌驱动
            self.price_label.setStyleSheet(f"color: {iv_color};")
            if iv_color == "white":
                self.interval_label.setStyleSheet("color: rgba(255,255,255,0.5);")
            else:
                self.interval_label.setStyleSheet(f"color: {iv_color};")
        else:
            self.interval_label.setText(f"{interval_min}min --")
            self.interval_label.setStyleSheet("color: rgba(255,255,255,0.5);")
            self.price_label.setStyleSheet("color: white;")

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
        painter.fillRect(self.rect(), QColor(30, 30, 30, 180))

        # 绘制价格曲线
        self._draw_sparkline(painter)

        # 画边框（必须重置 brush，否则会被曲线颜色填充）
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QColor(255, 255, 255, 30))
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

        # 判断涨跌颜色：与区间涨跌逻辑一致
        interval_min = self.cfg.get("interval_minutes", 5)
        threshold = self.cfg["color_threshold"]
        cutoff = t_max - interval_min * 60
        ref_price = None
        for ts, p, _ in history:
            if ts <= cutoff:
                ref_price = p
            else:
                break

        if ref_price is not None and ref_price > 0:
            iv_pct = (prices[-1] - ref_price) / ref_price * 100
            if iv_pct >= threshold:
                line_color = QColor(255, 68, 68, 180)     # 红
            elif iv_pct <= -threshold:
                line_color = QColor(68, 255, 68, 180)     # 绿
            else:
                line_color = QColor(255, 255, 255, 120)   # 白
        else:
            line_color = QColor(255, 255, 255, 120)

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
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_pos and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None

    def contextMenuEvent(self, event):
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

        action_quit = QAction("退出", self)
        action_quit.triggered.connect(QApplication.quit)
        menu.addAction(action_quit)
        menu.exec(event.globalPos())


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    widget = GoldWidget()
    widget.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

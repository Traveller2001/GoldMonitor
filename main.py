import platform
import subprocess
import sys
from typing import Optional

from PyQt6.QtCore import QPoint, QTimer, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QLabel,
    QMenu,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from api import fetch_gold_price
from settings import SettingsDialog, load_config


class PriceFetcher(QThread):
    price_fetched = pyqtSignal(object)

    def run(self):
        self.price_fetched.emit(fetch_gold_price())


class GoldWidget(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.last_price = None  # type: Optional[float]
        self.notified_high = False
        self.notified_low = False
        self._drag_pos = None  # type: Optional[QPoint]
        self._fetcher = None  # type: Optional[PriceFetcher]

        self._init_ui()
        self._init_tray()
        self._init_timer()
        self._fetch_price()

    def _init_ui(self):
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # macOS: 保持 Tool 窗口在应用失焦时仍然显示（非 Mac 自动忽略）
        self.setAttribute(Qt.WidgetAttribute.WA_MacAlwaysShowToolWindow, True)
        self.setFixedSize(200, 120)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 10)
        layout.setSpacing(1)

        title = QLabel("Au(T+D)")
        title.setFont(QFont("PingFang SC", 10))
        title.setStyleSheet("color: rgba(255,255,255,0.5);")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        self.price_label = QLabel("--")
        self.price_label.setFont(QFont("Menlo", 26, QFont.Weight.Bold))
        self.price_label.setStyleSheet("color: white;")
        self.price_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.price_label)

        self.change_label = QLabel("")
        self.change_label.setFont(QFont("Menlo", 11))
        self.change_label.setStyleSheet("color: rgba(255,255,255,0.5);")
        self.change_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.change_label)

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

        menu = QMenu()
        action_show = QAction("显示", self)
        action_show.triggered.connect(self._show_widget)
        menu.addAction(action_show)

        action_hide = QAction("隐藏", self)
        action_hide.triggered.connect(self.hide)
        menu.addAction(action_hide)

        menu.addSeparator()

        action_settings = QAction("设置", self)
        action_settings.triggered.connect(self._open_settings)
        menu.addAction(action_settings)

        action_refresh = QAction("刷新", self)
        action_refresh.triggered.connect(self._fetch_price)
        menu.addAction(action_refresh)

        menu.addSeparator()
        action_quit = QAction("退出", self)
        action_quit.triggered.connect(QApplication.quit)
        menu.addAction(action_quit)

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

    def _on_price(self, data):
        if data is None:
            return

        price = data["price"]
        prev = self.last_price
        self.last_price = price

        self.price_label.setText(f"¥{price:.2f}")

        if prev is not None and prev > 0:
            interval_pct = (price - prev) / prev * 100
            arrow = "▲" if interval_pct >= 0 else "▼"
            sign = "+" if interval_pct >= 0 else ""
            self.change_label.setText(f"{arrow} {sign}{interval_pct:.2f}%")

            threshold = self.cfg["color_threshold"]
            if interval_pct >= threshold:
                color = "#ff4444"
            elif interval_pct <= -threshold:
                color = "#44ff44"
            else:
                color = "white"

            self.price_label.setStyleSheet(f"color: {color};")
            if color == "white":
                self.change_label.setStyleSheet("color: rgba(255,255,255,0.5);")
            else:
                self.change_label.setStyleSheet(f"color: {color};")
        else:
            change = data["change"]
            change_pct = data["change_pct"]
            sign = "+" if change >= 0 else ""
            arrow = "▲" if change >= 0 else "▼"
            self.change_label.setText(f"{arrow} {sign}{change_pct:.2f}%")
            self.price_label.setStyleSheet("color: white;")

        self.range_label.setText(f"低 {data['low']:.2f} — 高 {data['high']:.2f}")
        self._check_notify(price)

    def _check_notify(self, price):
        high = self.cfg["notify_high"]
        low = self.cfg["notify_low"]

        if high > 0 and price >= high and not self.notified_high:
            self.notified_high = True
            self._send_notification(
                "金价突破高位",
                f"¥{price:.2f}/g 已达到 ≥ ¥{high:.2f} 的阈值",
            )
        elif high > 0 and price < high:
            self.notified_high = False

        if low > 0 and price <= low and not self.notified_low:
            self.notified_low = True
            self._send_notification(
                "金价跌破低位",
                f"¥{price:.2f}/g 已达到 ≤ ¥{low:.2f} 的阈值",
            )
        elif low > 0 and price > low:
            self.notified_low = False

    def _send_notification(self, title, body):
        try:
            if platform.system() == "Darwin":
                subprocess.run(
                    ["osascript", "-e",
                     f'display notification "{body}" with title "{title}" sound name "Glass"'],
                    check=False, capture_output=True,
                )
            elif platform.system() == "Linux":
                subprocess.run(
                    ["notify-send", title, body],
                    check=False, capture_output=True,
                )
            elif platform.system() == "Windows":
                # Windows 10+ toast via PowerShell
                ps = (
                    f"[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                    f"ContentType = WindowsRuntime] > $null; "
                    f"$xml = [xml]\"<toast><visual><binding template='ToastGeneric'>"
                    f"<text>{title}</text><text>{body}</text>"
                    f"</binding></visual></toast>\"; "
                    f"$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
                    f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('GoldMonitor').Show($toast)"
                )
                subprocess.run(
                    ["powershell", "-Command", ps],
                    check=False, capture_output=True,
                )
        except Exception:
            pass

    def _show_widget(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _open_settings(self):
        dlg = SettingsDialog(self)
        dlg.settings_changed.connect(self._apply_settings)
        dlg.exec()

    def _apply_settings(self, cfg):
        self.cfg = cfg
        self.timer.setInterval(cfg["refresh_interval"] * 1000)
        self.notified_high = False
        self.notified_low = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 16, 16)
        painter.setClipPath(path)
        painter.fillRect(self.rect(), QColor(30, 30, 30, 180))
        painter.setPen(QColor(255, 255, 255, 30))
        painter.drawPath(path)
        painter.end()

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
        menu.setStyleSheet("""
            QMenu {
                background: #2b2b2b; color: #e0e0e0;
                border: 1px solid #555; border-radius: 6px; padding: 4px;
            }
            QMenu::item:selected { background: #4a9eff; border-radius: 4px; }
        """)
        action_settings = QAction("设置", self)
        action_settings.triggered.connect(self._open_settings)
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

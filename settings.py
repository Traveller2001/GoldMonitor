import json
import os

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_CONFIG = {
    "refresh_interval": 30,
    "color_threshold": 0.5,
    "notify_high": 0.0,
    "notify_low": 0.0,
}


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            return {**DEFAULT_CONFIG, **cfg}
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


class SettingsDialog(QDialog):
    settings_changed = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("金价监控 - 设置")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.Tool)
        self.setFixedSize(360, 280)

        # 居中屏幕显示
        from PyQt6.QtWidgets import QApplication
        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(
                geo.x() + (geo.width() - 360) // 2,
                geo.y() + (geo.height() - 280) // 2,
            )
        self.setStyleSheet("""
            QDialog {
                background: #2b2b2b;
                color: #e0e0e0;
            }
            QLabel {
                color: #e0e0e0;
                font-size: 13px;
            }
            QSpinBox, QDoubleSpinBox {
                background: #3c3c3c;
                color: #e0e0e0;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 4px;
                font-size: 13px;
            }
            QPushButton {
                background: #4a9eff;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 24px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: #3a8eef;
            }
        """)

        cfg = load_config()

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(24, 20, 24, 20)

        form = QFormLayout()
        form.setSpacing(10)

        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(5, 300)
        self.spin_interval.setSuffix(" 秒")
        self.spin_interval.setValue(cfg["refresh_interval"])
        form.addRow("刷新间隔:", self.spin_interval)

        self.spin_color = QDoubleSpinBox()
        self.spin_color.setRange(0.01, 10.0)
        self.spin_color.setSuffix(" %")
        self.spin_color.setDecimals(2)
        self.spin_color.setValue(cfg["color_threshold"])
        form.addRow("涨跌变色阈值:", self.spin_color)

        self.spin_high = QDoubleSpinBox()
        self.spin_high.setRange(0, 99999)
        self.spin_high.setPrefix("¥ ")
        self.spin_high.setSuffix(" /g")
        self.spin_high.setDecimals(2)
        self.spin_high.setValue(cfg["notify_high"])
        form.addRow("高价通知 (≥):", self.spin_high)

        hint_high = QLabel("设为 0 表示不通知")
        hint_high.setStyleSheet("color: #888; font-size: 11px;")
        form.addRow("", hint_high)

        self.spin_low = QDoubleSpinBox()
        self.spin_low.setRange(0, 99999)
        self.spin_low.setPrefix("¥ ")
        self.spin_low.setSuffix(" /g")
        self.spin_low.setDecimals(2)
        self.spin_low.setValue(cfg["notify_low"])
        form.addRow("低价通知 (≤):", self.spin_low)

        hint_low = QLabel("设为 0 表示不通知")
        hint_low.setStyleSheet("color: #888; font-size: 11px;")
        form.addRow("", hint_low)

        layout.addLayout(form)

        btn_layout = QHBoxLayout()
        btn_save = QPushButton("保存")
        btn_save.clicked.connect(self._save)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_save)
        layout.addLayout(btn_layout)

    def _save(self):
        cfg = {
            "refresh_interval": self.spin_interval.value(),
            "color_threshold": self.spin_color.value(),
            "notify_high": self.spin_high.value(),
            "notify_low": self.spin_low.value(),
        }
        save_config(cfg)
        self.settings_changed.emit(cfg)
        self.accept()

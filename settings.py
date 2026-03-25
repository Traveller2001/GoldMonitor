import json
import os

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from glass import GlassDialog

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_CONFIG = {
    "refresh_interval": 30,
    "color_threshold": 0.5,
    "interval_minutes": 5,
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


class SettingsDialog(GlassDialog):
    settings_changed = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent, width=340, height=380)
        cfg = load_config()

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(28, 24, 28, 20)

        # 标题
        title = QLabel("设置")
        title.setFont(QFont("PingFang SC", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: white; background: transparent;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        layout.addSpacing(6)

        form = QFormLayout()
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(5, 300)
        self.spin_interval.setSuffix(" 秒")
        self.spin_interval.setValue(cfg["refresh_interval"])
        form.addRow("刷新间隔", self.spin_interval)

        self.spin_color = QDoubleSpinBox()
        self.spin_color.setRange(0.01, 10.0)
        self.spin_color.setSuffix(" %")
        self.spin_color.setDecimals(2)
        self.spin_color.setValue(cfg["color_threshold"])
        form.addRow("变色阈值", self.spin_color)

        self.spin_interval_min = QSpinBox()
        self.spin_interval_min.setRange(1, 120)
        self.spin_interval_min.setSuffix(" 分钟")
        self.spin_interval_min.setValue(cfg["interval_minutes"])
        form.addRow("区间时长", self.spin_interval_min)

        self.spin_high = QDoubleSpinBox()
        self.spin_high.setRange(0, 99999)
        self.spin_high.setPrefix("¥ ")
        self.spin_high.setSuffix(" /g")
        self.spin_high.setDecimals(2)
        self.spin_high.setValue(cfg["notify_high"])
        form.addRow("高价通知 ≥", self.spin_high)

        self.spin_low = QDoubleSpinBox()
        self.spin_low.setRange(0, 99999)
        self.spin_low.setPrefix("¥ ")
        self.spin_low.setSuffix(" /g")
        self.spin_low.setDecimals(2)
        self.spin_low.setValue(cfg["notify_low"])
        form.addRow("低价通知 ≤", self.spin_low)

        hint = QLabel("通知设为 0 表示关闭")
        hint.setStyleSheet("color: rgba(255,255,255,0.3); font-size: 11px; background: transparent;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        form.addRow("", hint)

        layout.addLayout(form)
        layout.addStretch()

        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        btn_cancel = QPushButton("取消")
        btn_cancel.setObjectName("closeBtn")
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(btn_cancel)

        btn_save = QPushButton("保存")
        btn_save.clicked.connect(self._save)
        btn_layout.addWidget(btn_save)

        layout.addLayout(btn_layout)

    def _save(self):
        cfg = {
            "refresh_interval": self.spin_interval.value(),
            "color_threshold": self.spin_color.value(),
            "interval_minutes": self.spin_interval_min.value(),
            "notify_high": self.spin_high.value(),
            "notify_low": self.spin_low.value(),
        }
        save_config(cfg)
        self.settings_changed.emit(cfg)
        self.accept()

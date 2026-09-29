"""Configuration: one typed schema drives defaults, validation and the dialog."""

import json
import math
import os
import tempfile
from typing import Any, Dict

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
)

from glass import GlassDialog, hint_label, section_label
from news_ai import DEFAULT_MODEL, EFFORTS

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")

# key: (default, kind, constraint). Numeric constraints are (low, high),
# strings have a max length and choices a tuple of allowed values.
SCHEMA = {
    "refresh_interval": (30, "int", (5, 300)),
    "color_threshold": (0.5, "float", (0.01, 10.0)),
    "interval_minutes": (5, "int", (1, 120)),
    "notify_high": (0.0, "float", (0.0, 99999.0)),
    "notify_low": (0.0, "float", (0.0, 99999.0)),
    "outlook_enabled": (True, "bool", None),
    "macro_notify": (True, "bool", None),
    "ai_enabled": (True, "bool", None),
    "deepseek_api_key": ("", "str", 256),
    "deepseek_model": (DEFAULT_MODEL, "str", 64),
    "ai_tag_effort": ("high", "choice", EFFORTS),
    "ai_deep_effort": ("high", "choice", EFFORTS),
    "ai_daily_token_budget": (0, "int", (0, 50_000_000)),
}
DEFAULT_CONFIG = {key: spec[0] for key, spec in SCHEMA.items()}
EFFORT_LABELS = {"off": "关闭思考", "low": "low", "high": "high", "max": "max"}


def _normalize_value(raw, default, kind, constraint):
    # type: (Any, Any, str, Any) -> Any
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)) and raw in (0, 1):
            return bool(raw)
        return default
    if kind == "str":
        return raw.strip()[:constraint] if isinstance(raw, str) else default
    if kind == "choice":
        return raw if raw in constraint else default
    try:
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise ValueError("invalid numeric value")
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("non-finite numeric value")
    except (ValueError, TypeError, OverflowError):
        value = default
    low, high = constraint
    value = max(low, min(high, value))
    return int(value) if kind == "int" else round(value, 2)


def _normalize_config(cfg):
    # type: (Any) -> Dict[str, Any]
    """Keep persisted values safe for the timers, the widgets and the network."""
    if not isinstance(cfg, dict):
        cfg = {}
    return {
        key: _normalize_value(cfg.get(key, default), default, kind, constraint)
        for key, (default, kind, constraint) in SCHEMA.items()
    }


def _thresholds_conflict(cfg):
    # type: (dict) -> bool
    return cfg["notify_high"] > 0 and cfg["notify_low"] >= cfg["notify_high"]


def load_config():
    # type: () -> dict
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = _normalize_config(json.load(f))
    except (OSError, ValueError, UnicodeError):
        return dict(DEFAULT_CONFIG)
    if _thresholds_conflict(cfg):
        # A hand-edited or old configuration must not trigger both alerts.
        cfg["notify_high"] = cfg["notify_low"] = 0.0
    return cfg


def save_config(cfg):
    # type: (dict) -> None
    cfg = _normalize_config(cfg)
    if _thresholds_conflict(cfg):
        raise ValueError("高价通知阈值必须大于低价通知阈值。")
    directory = os.path.dirname(os.path.abspath(CONFIG_PATH))
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=directory,
            prefix=".goldmonitor-config-", suffix=".tmp", delete=False,
        ) as f:
            temporary_path = f.name
            json.dump(cfg, f, indent=2, ensure_ascii=False, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, CONFIG_PATH)
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _spin(key, suffix="", prefix="", decimals=None):
    _, kind, (low, high) = SCHEMA[key]
    box = QDoubleSpinBox() if kind == "float" else QSpinBox()
    box.setRange(low, high)
    if decimals is not None:
        box.setDecimals(decimals)
    box.setSuffix(suffix)
    box.setPrefix(prefix)
    return box


class SettingsDialog(GlassDialog):
    settings_changed = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent, width=392, height=660, title="设置")
        self._cfg = load_config()
        cfg = self._cfg

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        form.addRow(section_label("行情"))
        self.spin_interval = _spin("refresh_interval", " 秒")
        form.addRow("刷新间隔", self.spin_interval)
        self.spin_color = _spin("color_threshold", " %", decimals=2)
        form.addRow("变色阈值", self.spin_color)
        self.spin_interval_min = _spin("interval_minutes", " 分钟")
        form.addRow("区间时长", self.spin_interval_min)

        form.addRow(section_label("通知"))
        self.spin_high = _spin("notify_high", " /g", "¥ ", decimals=2)
        form.addRow("高价通知 ≥", self.spin_high)
        self.spin_low = _spin("notify_low", " /g", "¥ ", decimals=2)
        form.addRow("低价通知 ≤", self.spin_low)
        self.check_macro_notify = QCheckBox("非农、CPI 等关键数据公布时通知")
        form.addRow("", self.check_macro_notify)

        form.addRow(section_label("宏观展望"))
        self.check_outlook = QCheckBox("显示宏观展望（利率预期 / 数据 / 新闻）")
        form.addRow("", self.check_outlook)
        self.check_ai = QCheckBox("用 DeepSeek 解读新闻")
        form.addRow("", self.check_ai)
        self.edit_key = QLineEdit()
        self.edit_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit_key.setPlaceholderText(
            "已读取环境变量 DEEPSEEK_API_KEY" if os.environ.get("DEEPSEEK_API_KEY") else "sk-…"
        )
        form.addRow("API Key", self.edit_key)
        self.combo_model = QComboBox()
        self.combo_model.setEditable(True)
        self.combo_model.addItems([DEFAULT_MODEL, "deepseek-v4-pro"])
        form.addRow("模型", self.combo_model)
        self.combo_tag_effort = self._effort_combo()
        form.addRow("快讯判读", self.combo_tag_effort)
        self.combo_deep_effort = self._effort_combo()
        form.addRow("深度解读", self.combo_deep_effort)
        self.spin_budget = _spin("ai_daily_token_budget", " tokens")
        self.spin_budget.setSingleStep(50_000)
        self.spin_budget.setSpecialValueText("不限")
        form.addRow("每日上限", self.spin_budget)
        self.body.addLayout(form)
        self.body.addWidget(hint_label(
            "价格通知设为 0 表示关闭。Key 优先读取环境变量；填在这里会保存在本地 config.json（已被 git 忽略）。"
        ))

        self.error_label = QLabel()
        self.error_label.setObjectName("error")
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        self.body.addWidget(self.error_label)
        self.body.addStretch()

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch()
        btn_cancel = QPushButton("取消")
        btn_cancel.setObjectName("secondary")
        btn_cancel.clicked.connect(self.reject)
        buttons.addWidget(btn_cancel)
        btn_save = QPushButton("保存")
        btn_save.setDefault(True)
        btn_save.clicked.connect(self._save)
        buttons.addWidget(btn_save)
        self.body.addLayout(buttons)

        self.spin_interval.setValue(cfg["refresh_interval"])
        self.spin_color.setValue(cfg["color_threshold"])
        self.spin_interval_min.setValue(cfg["interval_minutes"])
        self.spin_high.setValue(cfg["notify_high"])
        self.spin_low.setValue(cfg["notify_low"])
        self.check_macro_notify.setChecked(cfg["macro_notify"])
        self.check_outlook.setChecked(cfg["outlook_enabled"])
        self.check_ai.setChecked(cfg["ai_enabled"])
        self.edit_key.setText(cfg["deepseek_api_key"])
        self.combo_model.setCurrentText(cfg["deepseek_model"])
        self.combo_tag_effort.setCurrentIndex(EFFORTS.index(cfg["ai_tag_effort"]))
        self.combo_deep_effort.setCurrentIndex(EFFORTS.index(cfg["ai_deep_effort"]))
        self.spin_budget.setValue(cfg["ai_daily_token_budget"])
        self.check_outlook.toggled.connect(self._sync_enabled)
        self.check_ai.toggled.connect(self._sync_enabled)
        self._sync_enabled()

    @staticmethod
    def _effort_combo():
        combo = QComboBox()
        for effort in EFFORTS:
            combo.addItem(f"思考强度 {EFFORT_LABELS[effort]}" if effort != "off" else EFFORT_LABELS[effort], effort)
        return combo

    def _sync_enabled(self):
        outlook = self.check_outlook.isChecked()
        self.check_ai.setEnabled(outlook)
        ai = outlook and self.check_ai.isChecked()
        for widget in (self.edit_key, self.combo_model, self.combo_tag_effort,
                       self.combo_deep_effort, self.spin_budget):
            widget.setEnabled(ai)

    def _save(self):
        cfg = dict(self._cfg)
        cfg.update({
            "refresh_interval": self.spin_interval.value(),
            "color_threshold": self.spin_color.value(),
            "interval_minutes": self.spin_interval_min.value(),
            "notify_high": self.spin_high.value(),
            "notify_low": self.spin_low.value(),
            "macro_notify": self.check_macro_notify.isChecked(),
            "outlook_enabled": self.check_outlook.isChecked(),
            "ai_enabled": self.check_ai.isChecked(),
            "deepseek_api_key": self.edit_key.text().strip(),
            "deepseek_model": self.combo_model.currentText().strip() or DEFAULT_MODEL,
            "ai_tag_effort": self.combo_tag_effort.currentData(),
            "ai_deep_effort": self.combo_deep_effort.currentData(),
            "ai_daily_token_budget": self.spin_budget.value(),
        })
        cfg = _normalize_config(cfg)
        try:
            save_config(cfg)
        except (OSError, ValueError) as exc:
            self.error_label.setText(f"无法保存设置：{exc}")
            self.error_label.show()
            return
        self.error_label.clear()
        self.error_label.hide()
        self.settings_changed.emit(cfg)
        self.accept()

import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

LOG_PATH = os.path.join(os.path.dirname(__file__), "goldmonitor.log.jsonl")
RETENTION = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _prune_entries(entries: List[Dict]) -> List[Dict]:
    cutoff = _now() - RETENTION
    kept = []
    for entry in entries:
        ts = _parse_ts(entry.get("ts"))
        if ts is None or ts < cutoff:
            continue
        entry["ts"] = ts.isoformat(timespec="seconds")
        kept.append(entry)
    return kept


def _read_entries() -> List[Dict]:
    if not os.path.exists(LOG_PATH):
        return []

    entries = []
    try:
        with open(LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                entries.append(entry)
    except OSError:
        return []

    return entries


def _write_entries(entries: List[Dict]) -> None:
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            for entry in entries:
                json.dump(entry, f, ensure_ascii=False)
                f.write("\n")
    except OSError:
        pass


def load_recent_logs() -> List[Dict]:
    entries = _read_entries()
    pruned = _prune_entries(entries)
    if os.path.exists(LOG_PATH) and len(pruned) != len(entries):
        _write_entries(pruned)
    return pruned


def append_log(level: str, event: str, message: str) -> None:
    entry = {
        "ts": _now().isoformat(timespec="seconds"),
        "level": level.upper(),
        "event": event,
        "message": message,
    }
    entries = load_recent_logs()
    entries.append(entry)
    _write_entries(entries)


def format_logs(entries: List[Dict]) -> str:
    if not entries:
        return "最近 1 小时内暂无日志。"

    lines = []
    for entry in entries:
        ts = _parse_ts(entry.get("ts"))
        stamp = ts.astimezone().strftime("%H:%M:%S") if ts else "--:--:--"
        level = entry.get("level", "INFO")
        event = entry.get("event", "event")
        message = entry.get("message", "")
        lines.append(f"{stamp} [{level}] {event} {message}".rstrip())
    return "\n".join(lines)


class LogsDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("GoldMonitor - 日志")
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.Tool)
        self.resize(640, 420)

        screen = QApplication.primaryScreen()
        if screen:
            geo = screen.availableGeometry()
            self.move(
                geo.x() + (geo.width() - self.width()) // 2,
                geo.y() + (geo.height() - self.height()) // 2,
            )

        self.setStyleSheet(
            """
            QDialog {
                background: #202020;
                color: #e0e0e0;
            }
            QLabel {
                color: #cfcfcf;
                font-size: 12px;
            }
            QPlainTextEdit {
                background: #111111;
                color: #d8d8d8;
                border: 1px solid #444;
                border-radius: 6px;
                padding: 10px;
                font-family: Consolas, Menlo, monospace;
                font-size: 12px;
            }
            QPushButton {
                background: #4a9eff;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 8px 18px;
                font-size: 12px;
                font-weight: bold;
            }
            QPushButton:hover {
                background: #3a8eef;
            }
            """
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        layout.addWidget(QLabel("仅显示最近 1 小时内的运行日志"))

        self.editor = QPlainTextEdit(self)
        self.editor.setReadOnly(True)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.editor)

        button_row = QHBoxLayout()
        button_row.addStretch()

        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self.refresh_logs)
        button_row.addWidget(btn_refresh)

        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        button_row.addWidget(btn_close)
        layout.addLayout(button_row)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh_logs)
        self._refresh_timer.start(2000)

        self.refresh_logs()

    def refresh_logs(self) -> None:
        vbar = self.editor.verticalScrollBar()
        hbar = self.editor.horizontalScrollBar()
        prev_v = vbar.value()
        prev_h = hbar.value()
        was_at_bottom = prev_v >= max(0, vbar.maximum() - 4)

        self.editor.setPlainText(format_logs(load_recent_logs()))

        vbar = self.editor.verticalScrollBar()
        hbar = self.editor.horizontalScrollBar()
        if was_at_bottom:
            vbar.setValue(vbar.maximum())
        else:
            vbar.setValue(min(prev_v, vbar.maximum()))
        hbar.setValue(min(prev_h, hbar.maximum()))

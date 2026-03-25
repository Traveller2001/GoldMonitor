import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from glass import GlassDialog

LOG_PATH = os.path.join(os.path.dirname(__file__), "goldmonitor.log.jsonl")
RETENTION = timedelta(hours=1)


def _now():
    # type: () -> datetime
    return datetime.now().astimezone()


def _parse_ts(raw):
    # type: (Optional[str]) -> Optional[datetime]
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _prune_entries(entries):
    # type: (List[Dict]) -> List[Dict]
    cutoff = _now() - RETENTION
    kept = []
    for entry in entries:
        ts = _parse_ts(entry.get("ts"))
        if ts is None or ts < cutoff:
            continue
        entry["ts"] = ts.isoformat(timespec="seconds")
        kept.append(entry)
    return kept


def _read_entries():
    # type: () -> List[Dict]
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
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return entries


def _write_entries(entries):
    # type: (List[Dict]) -> None
    try:
        with open(LOG_PATH, "w", encoding="utf-8") as f:
            for entry in entries:
                json.dump(entry, f, ensure_ascii=False)
                f.write("\n")
    except OSError:
        pass


def load_recent_logs():
    # type: () -> List[Dict]
    entries = _read_entries()
    pruned = _prune_entries(entries)
    if os.path.exists(LOG_PATH) and len(pruned) != len(entries):
        _write_entries(pruned)
    return pruned


def append_log(level, event, message):
    # type: (str, str, str) -> None
    entry = {
        "ts": _now().isoformat(timespec="seconds"),
        "level": level.upper(),
        "event": event,
        "message": message,
    }
    entries = load_recent_logs()
    entries.append(entry)
    _write_entries(entries)


def format_logs(entries):
    # type: (List[Dict]) -> str
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


class LogsDialog(GlassDialog):
    def __init__(self, parent=None):
        super().__init__(parent, width=580, height=400)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(10)

        # 标题
        title = QLabel("运行日志")
        title.setFont(QFont("PingFang SC", 16, QFont.Weight.Bold))
        title.setStyleSheet("color: white; background: transparent;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        hint = QLabel("仅保留最近 1 小时")
        hint.setStyleSheet("color: rgba(255,255,255,0.3); font-size: 11px; background: transparent;")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        self.editor = QPlainTextEdit(self)
        self.editor.setReadOnly(True)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self.editor)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()

        btn_refresh = QPushButton("刷新")
        btn_refresh.clicked.connect(self.refresh_logs)
        btn_row.addWidget(btn_refresh)

        btn_close = QPushButton("关闭")
        btn_close.setObjectName("closeBtn")
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)

        layout.addLayout(btn_row)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh_logs)
        self._refresh_timer.start(2000)

        self.refresh_logs()

    def refresh_logs(self):
        vbar = self.editor.verticalScrollBar()
        prev_v = vbar.value()
        was_at_bottom = prev_v >= max(0, vbar.maximum() - 4)

        self.editor.setPlainText(format_logs(load_recent_logs()))

        vbar = self.editor.verticalScrollBar()
        if was_at_bottom:
            vbar.setValue(vbar.maximum())
        else:
            vbar.setValue(min(prev_v, vbar.maximum()))

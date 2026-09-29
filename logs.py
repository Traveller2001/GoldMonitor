import json
import os
import tempfile
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QHBoxLayout, QPlainTextEdit, QPushButton

from glass import GlassDialog, hint_label

LOG_PATH = os.path.join(os.path.dirname(__file__), "goldmonitor.log.jsonl")
RETENTION = timedelta(hours=1)
MAX_ENTRIES = 2000
MAX_LOG_BYTES = 2 * 1024 * 1024
CLEANUP_INTERVAL_SECONDS = 60
_LOG_LOCK = threading.RLock()
_last_cleanup_path = None
_last_cleanup_at = None


def _now():
    # type: () -> datetime
    return datetime.now().astimezone()


def _parse_ts(raw):
    # type: (Optional[str]) -> Optional[datetime]
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # Accept UTC suffixes on Python versions predating fromisoformat's Z support.
        ts = datetime.fromisoformat(raw[:-1] + "+00:00" if raw.endswith("Z") else raw)
        # Older log versions used local timestamps without an offset.
        return ts.astimezone() if ts.tzinfo is None else ts
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _text(value, default, limit):
    return value[:limit] if isinstance(value, str) else default


def _encode_entry(entry):
    return (json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n").encode(
        "utf-8", errors="replace"
    )


def _prune_entries(entries):
    # type: (List[Dict]) -> List[Dict]
    now = _now()
    cutoff = now - RETENTION
    kept = deque(maxlen=MAX_ENTRIES)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ts = _parse_ts(entry.get("ts"))
        if ts is None or ts < cutoff or ts > now:
            continue
        kept.append({
            "ts": ts.isoformat(timespec="seconds"),
            "level": _text(entry.get("level"), "INFO", 32),
            "event": _text(entry.get("event"), "event", 128),
            "message": _text(entry.get("message"), "", 4000),
        })
    # Keep the newest complete records within the disk and memory budget.
    bounded = []
    size = 0
    for entry in reversed(kept):
        size += len(_encode_entry(entry))
        if size > MAX_LOG_BYTES:
            break
        bounded.append(entry)
    return list(reversed(bounded))


def _read_entries():
    # type: () -> List[Dict]
    entries = deque(maxlen=MAX_ENTRIES)
    with _LOG_LOCK:
        try:
            with open(LOG_PATH, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                if size > MAX_LOG_BYTES:
                    f.seek(size - MAX_LOG_BYTES - 1)
                    if f.read(1) != b"\n":
                        f.readline()  # Discard the partial record at the start of the tail.
                for line in f:
                    try:
                        entry = json.loads(line)
                    except (ValueError, UnicodeError):
                        continue
                    if isinstance(entry, dict):
                        entries.append(entry)
        except OSError:
            return []
    return list(entries)


def _write_entries(entries):
    # type: (List[Dict]) -> None
    temporary_path = None
    with _LOG_LOCK:
        try:
            directory = os.path.dirname(os.path.abspath(LOG_PATH))
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=directory, prefix=".goldmonitor-logs-", delete=False,
            ) as f:
                temporary_path = f.name
                for entry in entries:
                    f.write(_encode_entry(entry))
            os.replace(temporary_path, LOG_PATH)
        except OSError:
            pass
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except OSError:
                    pass


def _log_size():
    try:
        return os.path.getsize(LOG_PATH)
    except OSError:
        return 0


def load_recent_logs():
    # type: () -> List[Dict]
    global _last_cleanup_path, _last_cleanup_at
    with _LOG_LOCK:
        entries = _read_entries()
        pruned = _prune_entries(entries)
        if pruned != entries or _log_size() > MAX_LOG_BYTES:
            _write_entries(pruned)
        _last_cleanup_path = os.path.abspath(LOG_PATH)
        _last_cleanup_at = time.monotonic()
        return pruned


def append_log(level, event, message):
    # type: (str, str, str) -> None
    entry = {
        "ts": _now().isoformat(timespec="seconds"),
        "level": _text(level, "INFO", 32).upper(),
        "event": _text(event, "event", 128),
        "message": _text(message, "", 4000),
    }
    with _LOG_LOCK:
        if (
            _last_cleanup_path != os.path.abspath(LOG_PATH)
            or _last_cleanup_at is None
            or time.monotonic() - _last_cleanup_at >= CLEANUP_INTERVAL_SECONDS
            or _log_size() > MAX_LOG_BYTES
        ):
            load_recent_logs()
        try:
            with open(LOG_PATH, "a+b") as f:
                # Recover gracefully from a partial last line after an interrupted write.
                f.seek(0, os.SEEK_END)
                if f.tell():
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        f.write(b"\n")
                f.write(_encode_entry(entry))
        except OSError:
            pass


def format_logs(entries):
    # type: (List[Dict]) -> str
    if not entries:
        return "最近 1 小时内暂无日志。"
    lines = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        ts = _parse_ts(entry.get("ts"))
        try:
            stamp = ts.astimezone().strftime("%H:%M:%S") if ts else "--:--:--"
        except (ValueError, OverflowError, OSError):
            stamp = "--:--:--"
        level = entry.get("level", "INFO")
        event = entry.get("event", "event")
        message = entry.get("message", "")
        lines.append(f"{stamp} [{level}] {event} {message}".rstrip())
    return "\n".join(lines) or "最近 1 小时内暂无日志。"


class LogsDialog(GlassDialog):
    def __init__(self, parent=None):
        super().__init__(parent, width=600, height=420, title="运行日志")
        self.close_button.clicked.disconnect()
        self.close_button.clicked.connect(self.close)
        self.body.addWidget(hint_label("仅保留最近 1 小时，每 2 秒自动刷新"))

        self.editor = QPlainTextEdit(self)
        self.editor.setReadOnly(True)
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.body.addWidget(self.editor, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(10)
        btn_row.addStretch()
        btn_refresh = QPushButton("刷新")
        btn_refresh.setObjectName("secondary")
        btn_refresh.clicked.connect(self.refresh_logs)
        btn_row.addWidget(btn_refresh)
        btn_close = QPushButton("关闭")
        btn_close.clicked.connect(self.close)
        btn_row.addWidget(btn_close)
        self.body.addLayout(btn_row)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self.refresh_logs)
        self._refresh_timer.start(2000)

        self.refresh_logs()

    def refresh_logs(self):
        vbar = self.editor.verticalScrollBar()
        prev_v = vbar.value()
        was_at_bottom = prev_v >= max(0, vbar.maximum() - 4)

        content = format_logs(load_recent_logs())
        if content == self.editor.toPlainText():
            return
        self.editor.setPlainText(content)

        vbar = self.editor.verticalScrollBar()
        if was_at_bottom:
            vbar.setValue(vbar.maximum())
        else:
            vbar.setValue(min(prev_v, vbar.maximum()))

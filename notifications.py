"""Deliver desktop notifications without blocking the Qt event loop."""

import base64
import html
import platform
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from PyQt6.QtCore import QObject, QProcess, QTimer, pyqtSignal


def _notification_command(title: str, body: str) -> Optional[List[str]]:
    system = platform.system()
    if system == "Darwin":
        # Pass user-visible strings as arguments, never as AppleScript source.
        script = (
            "on run argv\n"
            'display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"\n'
            "end run"
        )
        return ["osascript", "-e", script, "--", title, body]
    if system == "Linux":
        return ["notify-send", "--", title, body]
    if system == "Windows":
        xml = (
            "<toast><visual><binding template='ToastGeneric'><text>"
            + html.escape(title, quote=True)
            + "</text><text>"
            + html.escape(body, quote=True)
            + "</text></binding></visual></toast>"
        )
        # A PowerShell single-quoted literal prevents $/backtick interpolation.
        # Encode the script to preserve Unicode and quoting on Windows.
        xml_literal = "'" + xml.replace("'", "''") + "'"
        script = (
            "$ErrorActionPreference = 'Stop'; "
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
            "ContentType = WindowsRuntime] > $null; "
            "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
            "ContentType = WindowsRuntime] > $null; "
            "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument; "
            f"$xml.LoadXml({xml_literal}); "
            "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('GoldMonitor').Show($toast)"
        )
        encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        return ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
    return None


@dataclass
class _Delivery:
    process: QProcess
    timer: QTimer
    failed: bool = False


class NotificationDispatcher(QObject):
    """One in-flight command per key; accepted deliveries finish exactly once.

    ``send`` returns False when a key is busy, the OS is unsupported, or shutdown
    has started. Otherwise its outcome arrives through ``finished``. A successful
    outcome means the OS command exited successfully, not that the user read it.
    """

    finished = pyqtSignal(str, bool)

    def __init__(self, parent=None, timeout_ms=5000):
        super().__init__(parent)
        self._timeout_ms = max(1, min(int(timeout_ms), 5000))
        self._pending = {}  # type: Dict[str, _Delivery]
        self._stopping = False

    def send(self, key: str, title: str, body: str) -> bool:
        if self._stopping or key in self._pending:
            return False
        command = _notification_command(title, body)
        if not command:
            return False

        process = QProcess(self)
        process.setStandardOutputFile(QProcess.nullDevice())
        process.setStandardErrorFile(QProcess.nullDevice())
        timer = QTimer(process)
        timer.setSingleShot(True)
        delivery = _Delivery(process, timer)
        self._pending[key] = delivery
        process.finished.connect(
            lambda code, status: self._complete(
                key,
                delivery,
                not delivery.failed and code == 0 and status == QProcess.ExitStatus.NormalExit,
            )
        )
        process.errorOccurred.connect(lambda _error: self._on_error(key, delivery))
        timer.timeout.connect(lambda: self._on_timeout(key, delivery))
        timer.start(self._timeout_ms)
        process.start(command[0], command[1:])
        return True

    def _on_error(self, key: str, delivery: _Delivery):
        if self._pending.get(key) is not delivery:
            return
        delivery.failed = True
        delivery.timer.stop()
        if delivery.process.state() == QProcess.ProcessState.NotRunning:
            self._complete(key, delivery, False)
        else:
            delivery.process.kill()

    def _on_timeout(self, key: str, delivery: _Delivery):
        if self._pending.get(key) is not delivery:
            return
        delivery.failed = True
        delivery.process.kill()
        if delivery.process.state() == QProcess.ProcessState.NotRunning:
            self._complete(key, delivery, False)

    def _complete(self, key: str, delivery: _Delivery, success: bool):
        # FailedToStart / Crashed can be followed by finished; an old callback
        # must also leave a new delivery with the same key alone.
        if self._pending.get(key) is not delivery:
            return
        del self._pending[key]
        delivery.timer.stop()
        delivery.process.deleteLater()
        self.finished.emit(key, success)

    def shutdown(self):
        """Cancel active commands and allow at most one second to reap them."""
        if self._stopping:
            return
        self._stopping = True
        deliveries = list(self._pending.items())
        for _key, delivery in deliveries:
            delivery.failed = True
            delivery.timer.stop()
            delivery.process.kill()

        deadline = time.monotonic() + 1.0
        for key, delivery in deliveries:
            process = delivery.process
            if process.state() != QProcess.ProcessState.NotRunning:
                remaining_ms = max(0, int((deadline - time.monotonic()) * 1000))
                process.waitForFinished(remaining_ms)
            if process.state() == QProcess.ProcessState.NotRunning:
                self._complete(key, delivery, False)
            # In the unlikely event the OS has not reaped a killed process by
            # the deadline, keep its QObject alive until its finished signal.

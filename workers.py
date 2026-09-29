"""Background execution helpers that always hand results back to the UI thread."""

import threading
from typing import Any, Callable

from PyQt6.QtCore import QObject, QThread, pyqtSignal


def _error(prefix, exc):
    # type: (str, Exception) -> dict
    return {"ok": False, "error": f"{prefix}: {exc}"}


class TaskThread(QThread):
    """One blocking call on a QThread; ``result_ready`` fires exactly once.

    Used for the short, bounded quote requests that shutdown waits for.
    """

    result_ready = pyqtSignal(object)

    def __init__(self, task, error_prefix="后台任务异常", parent=None):
        # type: (Callable[[], Any], str, Any) -> None
        super().__init__(parent)
        self._task = task
        self._error_prefix = error_prefix

    def run(self):
        try:
            result = self._task()
        except Exception as exc:  # the UI must always get an answer
            result = _error(self._error_prefix, exc)
        self.result_ready.emit(result)


class BackgroundCall(QObject):
    """Run slow calls (macro sources, model requests) on daemon threads.

    Results arrive through ``result_ready`` on the owner's thread. A daemon
    thread that is still waiting on the network never blocks quitting, which
    matters for model calls that can take a minute.
    """

    result_ready = pyqtSignal(object)
    _delivered = pyqtSignal(object)

    def __init__(self, error_prefix="后台任务异常", parent=None):
        # type: (str, Any) -> None
        super().__init__(parent)
        self._error_prefix = error_prefix
        self.running = False
        self._delivered.connect(self._on_delivered)

    def start(self, task):
        # type: (Callable[[], Any]) -> bool
        if self.running:
            return False
        self.running = True
        threading.Thread(target=self._run, args=(task,), daemon=True, name="goldmonitor-bg").start()
        return True

    def _run(self, task):
        try:
            result = task()
        except Exception as exc:
            result = _error(self._error_prefix, exc)
        try:
            self._delivered.emit(result)
        except RuntimeError:
            pass  # the owner was destroyed while quitting

    def _on_delivered(self, result):
        self.running = False
        self.result_ready.emit(result)

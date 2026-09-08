import base64
import os
import sys
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QProcess, QTimer
from PyQt6.QtTest import QSignalSpy, QTest
from PyQt6.QtWidgets import QApplication

from notifications import NotificationDispatcher, _notification_command


class NotificationCommandTest(unittest.TestCase):
    def test_mac_passes_text_as_arguments(self):
        title = '-title "quote" \\ $value'
        body = "line one\nline two's text"
        with patch("notifications.platform.system", return_value="Darwin"):
            command = _notification_command(title, body)
        self.assertEqual(command[:2], ["osascript", "-e"])
        self.assertEqual(command[3:], ["--", title, body])
        self.assertNotIn(title, command[2])
        self.assertNotIn(body, command[2])

    def test_linux_ends_option_parsing_before_text(self):
        with patch("notifications.platform.system", return_value="Linux"):
            self.assertEqual(
                _notification_command("--title", "body ' \" $()`"),
                ["notify-send", "--", "--title", "body ' \" $()`"],
            )

    def test_windows_preserves_unicode_and_escapes_xml(self):
        with patch("notifications.platform.system", return_value="Windows"):
            command = _notification_command("黄金 <&'\"", "$value `text` 中文")
        self.assertEqual(command[:4], ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand"])
        script = base64.b64decode(command[4]).decode("utf-16-le")
        self.assertIn("黄金 &lt;&amp;&#x27;&quot;", script)
        self.assertIn("$value `text` 中文", script)
        self.assertIn("$xml.LoadXml('<toast>", script)
        self.assertIn("template=''ToastGeneric''", script)
        self.assertIn("$ErrorActionPreference = 'Stop'", script)


class NotificationDispatcherTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dispatcher = NotificationDispatcher()
        self.spy = QSignalSpy(self.dispatcher.finished)

    def tearDown(self):
        self.dispatcher.shutdown()
        self.dispatcher.deleteLater()
        self.app.processEvents()

    def send_python(self, code, key="high"):
        with patch("notifications._notification_command", return_value=[sys.executable, "-c", code]):
            return self.dispatcher.send(key, "title", "body")

    def await_result(self):
        if not self.spy:
            self.assertTrue(self.spy.wait(2000), "notification command did not finish")
        self.app.processEvents()

    def test_success_is_async_and_emitted_once(self):
        self.assertTrue(self.send_python("pass"))
        self.assertEqual(len(self.spy), 0)
        self.await_result()
        self.assertEqual(list(self.spy), [["high", True]])
        self.assertEqual(self.dispatcher._pending, {})

    def test_nonzero_exit_is_failure(self):
        self.send_python("raise SystemExit(3)")
        self.await_result()
        self.assertEqual(list(self.spy), [["high", False]])

    def test_missing_command_fails_once_and_key_can_be_retried(self):
        with patch("notifications._notification_command", return_value=["/definitely-missing-goldmonitor-command"]):
            self.assertTrue(self.dispatcher.send("high", "title", "body"))
        self.await_result()
        self.assertEqual(list(self.spy), [["high", False]])
        self.assertTrue(self.send_python("pass"))
        self.assertTrue(self.spy.wait(2000))
        self.assertEqual(list(self.spy), [["high", False], ["high", True]])

    def test_duplicate_key_is_rejected_without_replacing_process(self):
        self.assertTrue(self.send_python("import time; time.sleep(0.1)"))
        delivery = self.dispatcher._pending["high"]
        self.assertFalse(self.send_python("pass"))
        self.assertIs(self.dispatcher._pending["high"], delivery)
        self.await_result()
        self.assertEqual(list(self.spy), [["high", True]])

    def test_timeout_kills_process_and_keeps_event_loop_responsive(self):
        self.dispatcher._timeout_ms = 80
        ticks = []
        timer = QTimer()
        timer.timeout.connect(lambda: ticks.append(True))
        timer.start(10)
        self.send_python("import time; time.sleep(30)")
        self.await_result()
        timer.stop()
        QTest.qWait(30)
        self.assertTrue(ticks)
        self.assertEqual(list(self.spy), [["high", False]])
        self.assertEqual(self.dispatcher._pending, {})

    def test_retry_from_failure_signal_survives_old_process_callbacks(self):
        self.dispatcher._timeout_ms = 50
        accepted = []

        def retry(key, success):
            if not success:
                self.dispatcher._timeout_ms = 5000
                accepted.append(self.send_python("pass", key))

        self.dispatcher.finished.connect(retry)
        self.send_python("import time; time.sleep(30)")
        self.await_result()
        if len(self.spy) < 2:
            self.assertTrue(self.spy.wait(2000))
        self.app.processEvents()
        self.assertEqual(accepted, [True])
        self.assertEqual(list(self.spy), [["high", False], ["high", True]])
        self.assertEqual(self.dispatcher._pending, {})

    def test_shutdown_cancels_and_reaps_all_processes(self):
        self.send_python("import time; time.sleep(30)", "high")
        self.send_python("import time; time.sleep(30)", "low")
        processes = [delivery.process for delivery in self.dispatcher._pending.values()]
        start = time.monotonic()
        self.dispatcher.shutdown()
        elapsed = time.monotonic() - start
        self.assertLess(elapsed, 1.5)
        self.assertEqual(self.dispatcher._pending, {})
        self.assertTrue(all(process.state() == QProcess.ProcessState.NotRunning for process in processes))
        self.assertCountEqual(list(self.spy), [["high", False], ["low", False]])
        self.assertFalse(self.send_python("pass"))
        self.dispatcher.shutdown()
        self.assertEqual(len(self.spy), 2)

    def test_unsupported_platform_is_rejected(self):
        with patch("notifications.platform.system", return_value="unsupported"):
            self.assertFalse(self.dispatcher.send("high", "title", "body"))
        self.assertEqual(len(self.spy), 0)


if __name__ == "__main__":
    unittest.main()

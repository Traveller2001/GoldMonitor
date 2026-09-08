import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import logs


NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def entry(message, timestamp=NOW):
    return {"ts": timestamp.isoformat(), "level": "INFO", "event": "test", "message": message}


class LogPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "logs.jsonl"
        for name, value in (
            ("LOG_PATH", str(self.path)), ("_last_cleanup_at", None),
            ("_last_cleanup_path", None),
        ):
            patcher = patch.object(logs, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(logs, "_now", return_value=NOW)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_malformed_records_do_not_hide_valid_recent_logs(self):
        values = [
            [], None, 1, "invalid", {"ts": 10}, {"ts": "invalid"},
            entry("expired", NOW - timedelta(hours=2)),
            entry("future", NOW + timedelta(hours=2)), entry("valid"),
        ]
        self.path.write_text(
            "bad JSON\n" + "\n".join(json.dumps(value) for value in values),
            encoding="utf-8",
        )
        self.assertEqual([row["message"] for row in logs.load_recent_logs()], ["valid"])

    def test_legacy_naive_and_utc_suffix_timestamps_are_supported(self):
        local_naive = NOW.astimezone().replace(tzinfo=None)
        values = [entry("legacy", local_naive), {**entry("utc"), "ts": "2026-09-08T12:00:00Z"}]
        self.path.write_text("\n".join(json.dumps(value) for value in values), encoding="utf-8")
        recent = logs.load_recent_logs()
        self.assertEqual(len(recent), 2)
        self.assertTrue(all(logs._parse_ts(row["ts"]).tzinfo is not None for row in recent))

    def test_append_uses_append_io_between_cleanup_intervals(self):
        with patch.object(logs, "_read_entries", wraps=logs._read_entries) as read:
            with patch.object(logs, "_write_entries", wraps=logs._write_entries) as rewrite:
                for index in range(10):
                    logs.append_log("info", "test", str(index))
        self.assertEqual(read.call_count, 1)
        rewrite.assert_not_called()
        self.assertEqual(len(logs.load_recent_logs()), 10)

    def test_cleanup_removes_expired_entries_on_next_interval(self):
        logs.append_log("info", "test", "old")
        later = NOW + timedelta(hours=2)
        with patch.object(logs, "_now", return_value=later):
            with patch.object(logs, "_last_cleanup_at", -1000):
                logs.append_log("info", "test", "new")
            self.assertEqual([row["message"] for row in logs.load_recent_logs()], ["new"])

    def test_concurrent_appends_and_reads_preserve_every_entry(self):
        def write_batch(batch):
            for index in range(20):
                logs.append_log("info", "test", f"{batch}:{index}")
                if index % 5 == 0:
                    logs.load_recent_logs()

        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(write_batch, range(6)))
        recent = logs.load_recent_logs()
        self.assertEqual(len(recent), 120)
        self.assertEqual(len({row["message"] for row in recent}), 120)

    def test_incomplete_last_line_does_not_swallow_next_entry(self):
        self.path.write_text('{"incomplete":', encoding="utf-8")
        logs.append_log("INFO", "test", "recovered")
        self.assertEqual([row["message"] for row in logs.load_recent_logs()], ["recovered"])

    def test_byte_and_entry_limits_keep_latest_complete_records(self):
        self.path.write_text(
            "\n".join(json.dumps(entry(str(index) + "x" * 100)) for index in range(50)),
            encoding="utf-8",
        )
        with patch.object(logs, "MAX_LOG_BYTES", 1024), patch.object(logs, "MAX_ENTRIES", 3):
            recent = logs.load_recent_logs()
            self.assertEqual(len(recent), 3)
            self.assertTrue(recent[-1]["message"].startswith("49"))
            self.assertLessEqual(self.path.stat().st_size, 1024)

    def test_atomic_cleanup_failure_preserves_original_log(self):
        self.path.write_text(json.dumps(entry("original")) + "\n", encoding="utf-8")
        original = self.path.read_bytes()
        with patch.object(logs.os, "replace", side_effect=OSError("disk error")):
            logs._write_entries([entry("replacement")])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.path])

    def test_log_failure_does_not_crash_caller(self):
        with patch("builtins.open", side_effect=PermissionError("denied")):
            logs.append_log("INFO", "test", "message")
            self.assertEqual(logs.load_recent_logs(), [])

    def test_malformed_text_and_unicode_are_safe(self):
        self.path.write_text(json.dumps({**entry("\ud800"), "level": []}) + "\n", encoding="utf-8")
        recent = logs.load_recent_logs()
        self.assertEqual(recent[0]["level"], "INFO")
        self.assertIn("[INFO]", logs.format_logs(recent))
        self.assertEqual(logs.format_logs([[], None]), "最近 1 小时内暂无日志。")


if __name__ == "__main__":
    unittest.main()

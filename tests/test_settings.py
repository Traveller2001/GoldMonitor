import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

import settings


class SettingsPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "config.json"
        patcher = patch.object(settings, "CONFIG_PATH", str(self.path))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_or_malformed_config_returns_independent_defaults(self):
        for content in (None, "{", "[]", '"invalid"', "null"):
            with self.subTest(content=content):
                if content is not None:
                    self.path.write_text(content, encoding="utf-8")
                cfg = settings.load_config()
                self.assertEqual(cfg, settings.DEFAULT_CONFIG)
                self.assertIsNot(cfg, settings.DEFAULT_CONFIG)

    def test_numeric_strings_and_out_of_range_values_are_normalized(self):
        self.path.write_text(json.dumps({
            "refresh_interval": "2", "color_threshold": 100,
            "interval_minutes": "20", "notify_high": 100000,
            "notify_low": -1,
        }), encoding="utf-8")
        self.assertEqual(settings.load_config(), {
            **settings.DEFAULT_CONFIG,
            "refresh_interval": 5, "color_threshold": 10.0,
            "interval_minutes": 20, "notify_high": 99999.0,
            "notify_low": 0.0,
        })

    def test_invalid_types_and_nonfinite_values_use_defaults(self):
        numeric = [key for key, spec in settings.SCHEMA.items() if spec[1] in ("int", "float")]
        for invalid in (True, None, [], {}, "invalid", float("inf"), float("nan")):
            with self.subTest(invalid=invalid):
                self.path.write_text(json.dumps(dict.fromkeys(numeric, invalid)), encoding="utf-8")
                self.assertEqual(settings.load_config(), settings.DEFAULT_CONFIG)

    def test_switches_text_and_choices_are_validated(self):
        self.path.write_text(json.dumps({
            "outlook_enabled": 0, "macro_notify": "yes", "ai_enabled": [],
            "deepseek_api_key": "  sk-abc  ", "deepseek_model": 5,
            "ai_tag_effort": "off", "ai_deep_effort": "extreme", "ai_daily_token_budget": -5,
        }), encoding="utf-8")
        cfg = settings.load_config()
        self.assertEqual((cfg["outlook_enabled"], cfg["macro_notify"], cfg["ai_enabled"]), (False, True, True))
        self.assertEqual((cfg["deepseek_api_key"], cfg["deepseek_model"]), ("sk-abc", "deepseek-flash"))
        self.assertEqual((cfg["ai_tag_effort"], cfg["ai_deep_effort"]), ("off", "high"))
        self.assertEqual(cfg["ai_daily_token_budget"], 0)

    def test_unknown_keys_from_older_versions_are_dropped(self):
        self.path.write_text('{"refresh_interval": 60, "legacy": true}', encoding="utf-8")
        self.assertNotIn("legacy", settings.load_config())

    def test_conflicting_persisted_alerts_are_disabled(self):
        self.path.write_text('{"notify_high": 700, "notify_low": 800}', encoding="utf-8")
        cfg = settings.load_config()
        self.assertEqual((cfg["notify_high"], cfg["notify_low"]), (0.0, 0.0))

    def test_atomic_save_failure_preserves_previous_configuration(self):
        self.path.write_text('{"refresh_interval": 60}', encoding="utf-8")
        previous = self.path.read_bytes()
        with patch.object(settings.os, "replace", side_effect=OSError("disk error")):
            with self.assertRaises(OSError):
                settings.save_config(settings.DEFAULT_CONFIG)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [self.path])

    def test_round_trip_and_conflicting_alert_rejection(self):
        cfg = {**settings.DEFAULT_CONFIG, "notify_high": 800.0, "notify_low": 700.0}
        settings.save_config(cfg)
        self.assertEqual(settings.load_config(), cfg)
        with self.assertRaises(ValueError):
            settings.save_config({**cfg, "notify_low": 800})
        self.assertEqual(settings.load_config(), cfg)


class SettingsDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        with patch.object(settings, "load_config", return_value=dict(settings.DEFAULT_CONFIG)):
            self.dialog = settings.SettingsDialog()
        self.addCleanup(self.dialog.close)
        self.saved = []
        self.dialog.settings_changed.connect(self.saved.append)

    def test_save_error_keeps_dialog_open_without_emitting_changes(self):
        with patch.object(settings, "save_config", side_effect=OSError("read only")):
            self.dialog._save()
        self.assertEqual(self.saved, [])
        self.assertIn("read only", self.dialog.error_label.text())
        self.assertFalse(self.dialog.error_label.isHidden())
        self.assertEqual(self.dialog.result(), 0)

    def test_conflicting_alerts_show_inline_validation(self):
        self.dialog.spin_high.setValue(700)
        self.dialog.spin_low.setValue(800)
        self.dialog._save()
        self.assertEqual(self.saved, [])
        self.assertIn("必须大于", self.dialog.error_label.text())

    def test_successful_save_emits_changes(self):
        self.dialog.spin_interval.setValue(45)
        with patch.object(settings, "save_config") as save:
            self.dialog._save()
        self.assertEqual(self.saved[0]["refresh_interval"], 45)
        save.assert_called_once_with(self.saved[0])
        self.assertEqual(self.dialog.result(), 1)

    def test_ai_options_are_saved_and_disabled_with_the_outlook(self):
        self.dialog.edit_key.setText(" sk-new ")
        self.dialog.combo_tag_effort.setCurrentIndex(settings.EFFORTS.index("low"))
        self.dialog.spin_budget.setValue(200000)
        with patch.object(settings, "save_config"):
            self.dialog._save()
        saved = self.saved[0]
        self.assertEqual((saved["deepseek_api_key"], saved["ai_tag_effort"], saved["ai_deep_effort"]),
                         ("sk-new", "low", "high"))
        self.assertEqual(saved["ai_daily_token_budget"], 200000)
        self.dialog.check_outlook.setChecked(False)
        self.assertFalse(self.dialog.check_ai.isEnabled())
        self.assertFalse(self.dialog.edit_key.isEnabled())


if __name__ == "__main__":
    unittest.main()

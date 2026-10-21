from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from seminar_bot.__main__ import instance_lock
from seminar_bot.rotation import apply_rotation, export_rotations, read_rotations
from seminar_bot.store import Store, UserError

DAY = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, 0, tzinfo=UTC)  # 09:00 KST


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "rotation.json"
        self.store = Store(self.root / "seminar.sqlite3")
        self.addCleanup(lambda: self.store.close())
        self.store.configure(1, 1001, 1002, 3, "09:00", "18:00", "Asia/Seoul", NOW)

    def write(self, ids=(101, 102), start=None, name="관리용 이름"):
        data = {
            "guilds": {
                "1": {
                    "start_date": start,
                    "presenters": [{"user_id": str(i), "name": name} for i in ids],
                }
            }
        }
        self.path.write_text(json.dumps(data, ensure_ascii=False))
        return read_rotations(self.path)[0]

    def test_initial_file_repeats_presenters(self):
        rotation = self.write()
        self.assertEqual(apply_rotation(self.store, rotation, NOW), DAY)
        self.assertEqual(
            [row["presenter_id"] for row in self.store.upcoming(1, DAY, 4)],
            [101, 102, 101, 102],
        )

    def test_reorder_changes_future_occurrences(self):
        apply_rotation(self.store, self.write(), NOW)
        apply_rotation(self.store, self.write((102, 103, 101)), NOW)
        self.assertEqual(
            [row["presenter_id"] for row in self.store.upcoming(1, DAY, 4)],
            [102, 103, 101, 102],
        )

    def test_unchanged_file_and_restart_preserve_swaps_and_holidays(self):
        rotation = self.write()
        apply_rotation(self.store, rotation, NOW)
        self.store.swap(1, DAY, DAY + timedelta(weeks=1), DAY)
        self.store.postpone(1, DAY)
        before = self.store.upcoming(1, DAY)
        self.store.close()
        self.store = Store(self.root / "seminar.sqlite3")
        self.assertIsNone(apply_rotation(self.store, rotation, NOW + timedelta(weeks=2)))
        self.assertEqual(self.store.upcoming(1, DAY), before)
        self.assertEqual(self.store.holidays(1, DAY)[0]["seminar_date"], str(DAY))

    def test_display_name_and_whitespace_changes_do_not_reset_schedule(self):
        apply_rotation(self.store, self.write(), NOW)
        self.store.postpone(1, DAY)
        before = self.store.upcoming(1, DAY)
        self.assertIsNone(apply_rotation(self.store, self.write(name="새 이름"), NOW))
        self.assertEqual(self.store.upcoming(1, DAY), before)

    def test_explicit_future_start_preserves_earlier_sessions(self):
        apply_rotation(self.store, self.write(), NOW)
        original = self.store.session_on(1, DAY)
        start = DAY + timedelta(weeks=2)
        apply_rotation(self.store, self.write((103,), str(start)), NOW)
        self.assertEqual(self.store.session_on(1, DAY), original)
        self.assertEqual(self.store.session_on(1, start)["presenter_id"], 103)

    def test_default_start_skips_today_after_reminder(self):
        apply_rotation(self.store, self.write(), NOW)
        old = self.store.session_on(1, DAY)
        self.store.mark_sent(old["id"], str(DAY), "morning", 900)
        self.assertEqual(
            apply_rotation(self.store, self.write((103,)), NOW), DAY + timedelta(weeks=1)
        )
        self.assertEqual(self.store.session_on(1, DAY), old)

    def test_default_start_skips_today_after_evening(self):
        self.assertEqual(
            apply_rotation(self.store, self.write(), NOW + timedelta(hours=10)),
            DAY + timedelta(weeks=1),
        )

    def test_default_start_keeps_existing_holiday(self):
        apply_rotation(self.store, self.write(), NOW)
        self.store.postpone(1, DAY)
        self.assertEqual(
            apply_rotation(self.store, self.write((103,)), NOW), DAY + timedelta(weeks=1)
        )
        self.assertIsNone(self.store.session_on(1, DAY))

    def test_changed_file_does_not_replace_protected_sessions(self):
        apply_rotation(self.store, self.write(), NOW)
        future = DAY + timedelta(weeks=1)
        session = self.store.session_on(1, future)
        self.store.mark_sent(session["id"], str(future), "morning", 900)
        original = self.store.upcoming(1, DAY)
        fingerprint = self.store.rotation_fingerprint(1)
        with self.assertRaises(UserError):
            apply_rotation(self.store, self.write((103,)), NOW)
        self.assertEqual(self.store.upcoming(1, DAY), original)
        self.assertEqual(self.store.rotation_fingerprint(1), fingerprint)

    def test_export_adopts_legacy_state_without_resetting_schedule(self):
        self.store.set_rotation(1, [101, 102], DAY, DAY)
        self.store.swap(1, DAY, DAY + timedelta(weeks=1), DAY)
        self.store.postpone(1, DAY)
        original = self.store.upcoming(1, DAY)
        data = export_rotations(self.store, NOW)
        self.path.write_text(json.dumps(data))
        rotation = read_rotations(self.path)[0]
        self.assertIsNone(apply_rotation(self.store, rotation, NOW))
        self.assertEqual(self.store.upcoming(1, DAY), original)
        self.assertEqual(len(self.store.holidays(1, DAY)), 1)

    def test_invalid_file_is_rejected_without_changing_store(self):
        apply_rotation(self.store, self.write(), NOW)
        original = self.store.upcoming(1, DAY)
        for content in ("{", "[]", '{"guilds": {}, "guilds": {}}', '{"guilds": []}'):
            with self.subTest(content=content):
                self.path.write_text(content)
                with self.assertRaises(UserError):
                    read_rotations(self.path)
                self.assertEqual(self.store.upcoming(1, DAY), original)

    def test_extremely_large_json_number_is_rejected_safely(self):
        self.path.write_text('{"guilds": {"1": ' + "1" * 5000 + "}}")
        with self.assertRaises(UserError):
            read_rotations(self.path)

    def test_export_cli_outputs_json_and_refuses_a_running_instance(self):
        self.store.set_rotation(1, [101, 102], DAY, DAY)
        command = [sys.executable, "-m", "seminar_bot.rotation"]
        env = dict(os.environ, DATA_DIR=str(self.root))
        result = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
        self.assertEqual(len(json.loads(result.stdout)["guilds"]["1"]["presenters"]), 2)
        with instance_lock(self.root):
            result = subprocess.run(command, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")

    def test_ids_and_dates_are_validated(self):
        for ids, start in (
            ((101, 101), None),
            ((0,), None),
            ((-1,), None),
            ((2**63,), None),
            ((), None),
            ((101,), "2026-99-99"),
            ((101,), True),
        ):
            with self.subTest(ids=ids, start=start), self.assertRaises(UserError):
                self.write(ids, start)
        self.write()
        data = json.loads(self.path.read_text())
        data["guilds"]["1"]["presenters"][0]["user_id"] = 101
        self.path.write_text(json.dumps(data))
        with self.assertRaises(UserError):
            read_rotations(self.path)

    def test_past_or_wrong_weekday_start_is_rejected(self):
        for start in (DAY - timedelta(weeks=1), DAY + timedelta(days=1)):
            with self.subTest(start=start), self.assertRaises(UserError):
                apply_rotation(self.store, self.write(start=str(start)), NOW)

    def test_missing_or_oversized_file_is_rejected(self):
        with self.assertRaises(UserError):
            read_rotations(self.path)
        self.path.write_bytes(b" " * (256 * 1024 + 1))
        with self.assertRaises(UserError):
            read_rotations(self.path)


if __name__ == "__main__":
    unittest.main()

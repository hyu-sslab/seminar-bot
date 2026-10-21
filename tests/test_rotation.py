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
from seminar_bot.submission_access import SubmissionAccess

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

    def write_flat(self, ids=(101, 102), start=None, name="관리용 이름", guild_id=1):
        self.path.write_text(
            json.dumps(
                {
                    "start_date": start,
                    "presenters": [{"user_id": str(user_id), "name": name} for user_id in ids],
                },
                ensure_ascii=False,
            )
        )
        return read_rotations(self.path, default_guild_id=guild_id)[0]

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
        self.assertEqual(set(data), {"start_date", "presenters"})
        self.path.write_text(json.dumps(data))
        rotation = read_rotations(self.path, default_guild_id=1)[0]
        self.assertEqual(self.store.rotation_fingerprint(1), rotation.fingerprint)
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
        env = dict(os.environ, DATA_DIR=str(self.root), SEMINAR_GUILD_ID="", DISCORD_GUILD_ID="")
        result = subprocess.run(command, env=env, capture_output=True, text=True, check=True)
        exported = json.loads(result.stdout)
        self.assertEqual(set(exported), {"start_date", "presenters"})
        self.assertIsNone(exported["start_date"])
        self.assertEqual(len(exported["presenters"]), 2)
        self.path.write_text(result.stdout)
        rotation = read_rotations(self.path, default_guild_id=1)[0]
        original = self.store.upcoming(1, DAY)
        self.assertIsNone(apply_rotation(self.store, rotation, NOW))
        self.assertEqual(self.store.upcoming(1, DAY), original)
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

    def test_flat_format_requires_an_unambiguous_valid_integer_guild(self):
        self.write_flat()
        with self.assertRaises(UserError):
            read_rotations(self.path)
        for guild_id in (None, 0, -1, 2**63, True, False, "1", 1.0, [], {}):
            with self.subTest(guild_id=guild_id), self.assertRaises(UserError):
                read_rotations(self.path, default_guild_id=guild_id)
        for guild_id in (1, 2**63 - 1):
            rotation = read_rotations(self.path, default_guild_id=guild_id)[0]
            self.assertEqual(rotation.guild_id, guild_id)
            self.assertEqual(rotation.presenters, (101, 102))
        # Explicit legacy server IDs never require implicit server selection.
        self.assertEqual(self.write().guild_id, 1)
        self.assertEqual(read_rotations(self.path, default_guild_id=2)[0].guild_id, 1)

    def test_flat_and_legacy_formats_have_identical_schedule_fingerprints(self):
        for start in (None, str(DAY)):
            with self.subTest(start=start):
                legacy = self.write(start=start, name="이름 A")
                flat = self.write_flat(start=start, name="이름 B")
                self.assertEqual(
                    (flat.guild_id, flat.presenters, flat.start_date),
                    (legacy.guild_id, legacy.presenters, legacy.start_date),
                )
                self.assertEqual(flat.fingerprint, legacy.fingerprint)

    def test_changing_only_to_flat_format_preserves_holidays_swaps_and_submission_grants(self):
        legacy = self.write()
        apply_rotation(self.store, legacy, NOW)
        self.store.swap(1, DAY, DAY + timedelta(weeks=1), DAY)
        self.store.postpone(1, DAY)
        session = self.store.upcoming(1, DAY, 1)[0]
        access = SubmissionAccess(self.store)
        invite, _ = access.issue(
            1, session["id"], session["presenter_id"], now=1_700_000_000, scope="presenter"
        )
        token, _ = access.redeem(invite, 1, now=1_700_000_001)
        original = self.store.upcoming(1, DAY)
        holidays = self.store.holidays(1, DAY)
        fingerprint = self.store.rotation_fingerprint(1)
        flat = self.write_flat(name="표시 이름 변경")
        self.assertIsNone(apply_rotation(self.store, flat, NOW + timedelta(weeks=2)))
        self.assertEqual(self.store.upcoming(1, DAY), original)
        self.assertEqual(self.store.holidays(1, DAY), holidays)
        self.assertEqual(self.store.rotation_fingerprint(1), fingerprint)
        self.assertEqual(
            access.validate(token, 1, now=1_700_000_002)["presenter_id"], session["presenter_id"]
        )

    def test_flat_format_rejects_unknown_mixed_and_duplicate_keys(self):
        entry = {"start_date": None, "presenters": [{"user_id": "101", "name": "발표자"}]}
        for payload in (
            {**entry, "guild_id": "1"},
            {**entry, "guilds": {"1": entry}},
            {"guilds": {"1": entry}, "start_date": None},
            {"guilds": {"1": {**entry, "timezone": "UTC"}}},
            {"presenters": entry["presenters"], "unexpected": True},
        ):
            with self.subTest(payload=payload):
                self.path.write_text(json.dumps(payload))
                with self.assertRaises(UserError):
                    read_rotations(self.path, default_guild_id=1)
        self.path.write_text('{"start_date":null,"presenters":[],"presenters":[]}')
        with self.assertRaises(UserError):
            read_rotations(self.path, default_guild_id=1)

    def test_flat_presenter_and_start_boundaries_match_legacy_validation(self):
        for payload in (
            {"presenters": []},
            {"presenters": [{"user_id": str(i)} for i in range(1, 102)]},
            {"presenters": [{"user_id": "101"}, {"user_id": "0101"}]},
            {"presenters": [{"user_id": 101}]},
            {"presenters": [{"user_id": True}]},
            {"presenters": [{"user_id": "0"}]},
            {"presenters": [{"user_id": str(2**63)}]},
            {"presenters": [{"user_id": "101", "name": "x" * 101}]},
            {"presenters": [{"user_id": "101", "name": None}]},
            {"presenters": [{"user_id": "101", "role": "speaker"}]},
            {"presenters": ["101"]},
            {"presenters": {}},
            {"presenters": [{"user_id": "101"}], "start_date": True},
            {"presenters": [{"user_id": "101"}], "start_date": "2026-02-30"},
        ):
            with self.subTest(payload=payload):
                self.path.write_text(json.dumps(payload))
                with self.assertRaises(UserError):
                    read_rotations(self.path, default_guild_id=1)
        rotation = self.write_flat(ids=range(1, 101), name="x" * 100)
        self.assertEqual(len(rotation.presenters), 100)
        self.assertEqual(rotation.names, ("x" * 100,) * 100)
        self.path.write_text('{"presenters":[{"user_id":"101"}]}')
        self.assertIsNone(read_rotations(self.path, default_guild_id=1)[0].start_date)
        self.path.write_text('{"guilds":{}}')
        self.assertEqual(read_rotations(self.path), [])

    def test_export_without_active_presenters_produces_an_empty_single_server_template(self):
        self.assertEqual(export_rotations(self.store, NOW), {"start_date": None, "presenters": []})
        with tempfile.TemporaryDirectory() as directory:
            empty = Store(Path(directory) / "empty.sqlite3")
            try:
                self.assertEqual(
                    export_rotations(empty, NOW), {"start_date": None, "presenters": []}
                )
            finally:
                empty.close()

    def test_export_keeps_explicit_guilds_when_multiple_configured_servers_have_one_rotation(self):
        self.store.set_rotation(1, [101, 102], DAY, DAY)
        self.store.configure(2, 2001, 2002, 3, "09:00", "18:00", "Asia/Seoul", NOW)
        original = self.store.upcoming(1, DAY)
        data = export_rotations(self.store, NOW)
        self.assertEqual(set(data), {"guilds"})
        self.assertEqual(set(data["guilds"]), {"1"})
        self.path.write_text(json.dumps(data))
        rotations = read_rotations(self.path)
        self.assertEqual(len(rotations), 1)
        self.assertIsNone(apply_rotation(self.store, rotations[0], NOW))
        self.assertEqual(self.store.upcoming(1, DAY), original)
        self.assertEqual(self.store.upcoming(2, DAY), [])

    def test_export_cli_uses_seminar_guild_without_changing_stale_server_state(self):
        self.store.set_rotation(1, [101, 102], DAY, DAY)
        self.store.configure(2, 2001, 2002, 3, "09:00", "18:00", "Asia/Seoul", NOW)
        self.store.set_rotation(2, [201, 202], DAY, DAY)
        self.store.postpone(1, DAY)
        active_sessions = self.store.upcoming(1, DAY)
        stale = self.store.require_config(2)
        stale_sessions = self.store.upcoming(2, DAY)
        stale_fingerprint = self.store.rotation_fingerprint(2)
        result = subprocess.run(
            [sys.executable, "-m", "seminar_bot.rotation"],
            env=dict(
                os.environ, DATA_DIR=str(self.root), SEMINAR_GUILD_ID="1", DISCORD_GUILD_ID="2"
            ),
            capture_output=True,
            text=True,
            check=True,
        )
        exported = json.loads(result.stdout)
        self.assertEqual(set(exported), {"start_date", "presenters"})
        self.assertEqual({p["user_id"] for p in exported["presenters"]}, {"101", "102"})
        self.path.write_text(result.stdout)
        rotation = read_rotations(self.path, default_guild_id=1)[0]
        self.assertIsNone(apply_rotation(self.store, rotation, NOW))
        self.assertEqual(self.store.upcoming(1, DAY), active_sessions)
        self.assertEqual(self.store.require_config(2), stale)
        self.assertEqual(self.store.upcoming(2, DAY), stale_sessions)
        self.assertEqual(self.store.rotation_fingerprint(2), stale_fingerprint)

    def test_export_of_multiple_active_servers_preserves_each_schedule_and_fingerprint(self):
        self.store.set_rotation(1, [101, 102], DAY, DAY)
        self.store.configure(2, 2001, 2002, 3, "09:00", "18:00", "Asia/Seoul", NOW)
        self.store.set_rotation(2, [201, 202], DAY, DAY)
        self.store.swap(1, DAY, DAY + timedelta(weeks=1), DAY)
        self.store.postpone(2, DAY)
        before = {guild: self.store.upcoming(guild, DAY) for guild in (1, 2)}
        data = export_rotations(self.store, NOW)
        self.assertEqual(set(data), {"guilds"})
        self.assertEqual(set(data["guilds"]), {"1", "2"})
        self.path.write_text(json.dumps(data))
        rotations = read_rotations(self.path)
        self.assertEqual({rotation.guild_id for rotation in rotations}, {1, 2})
        for rotation in rotations:
            self.assertEqual(
                self.store.rotation_fingerprint(rotation.guild_id), rotation.fingerprint
            )
            self.assertIsNone(apply_rotation(self.store, rotation, NOW))
            self.assertEqual(self.store.upcoming(rotation.guild_id, DAY), before[rotation.guild_id])


if __name__ == "__main__":
    unittest.main()

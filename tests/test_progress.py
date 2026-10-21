from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from seminar_bot.__main__ import instance_lock
from seminar_bot.documents import DocumentInfo
from seminar_bot.progress import align_progress
from seminar_bot.rotation import apply_rotation, read_rotations
from seminar_bot.store import Store, UserError, next_weekday
from seminar_bot.submission_access import SubmissionAccess, SubmissionAccessDenied

LAST = date(2026, 5, 7)
FIRST = LAST + timedelta(weeks=1)
TODAY = FIRST - timedelta(days=1)
NOW = datetime.combine(TODAY, datetime.min.time(), UTC)


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db_path = self.root / "seminar.sqlite3"
        self.rotation_path = self.root / "rotation.json"
        self.store = Store(self.db_path)
        self.addCleanup(lambda: self.store.close())
        self.store.configure(1, 1001, 1002, 3, "09:00", "18:00", "UTC", NOW, seminar_time="16:00")
        self.store.set_rotation(1, [101, 102, 103], FIRST, TODAY)
        self.rotation_path.write_text(
            json.dumps(
                {
                    "start_date": None,
                    "presenters": [
                        {"user_id": str(user_id), "name": f"Presenter {user_id}"}
                        for user_id in (101, 102, 103)
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.original_json = self.rotation_path.read_bytes()
        self.rotation = read_rotations(self.rotation_path, default_guild_id=1)[0]

    def unchanged(self):
        if self.rotation_path.read_bytes() != self.original_json:
            raise UserError("발표 순서 파일이 변경되었습니다.")

    def align(self):
        return align_progress(self.store, self.rotation, LAST, 101, TODAY, self.unchanged)

    def snapshot(self):
        return list(self.store.db.iterdump())

    def test_anchor_aligns_next_presenter_preserving_ids_order_and_file(self):
        existing = {row["seminar_date"]: row["id"] for row in self.store.upcoming(1, TODAY, 12)}
        self.assertEqual(self.align(), (FIRST, 102))
        self.assertEqual(self.store.session_on(1, LAST)["presenter_id"], 101)
        upcoming = self.store.upcoming(1, TODAY, 12)
        self.assertEqual([row["presenter_id"] for row in upcoming[:4]], [102, 103, 101, 102])
        self.assertEqual({row["seminar_date"]: row["id"] for row in upcoming}, existing)
        self.assertEqual(self.rotation_path.read_bytes(), self.original_json)
        config = self.store.require_config(1)
        self.assertEqual(json.loads(config["rotation"]), [101, 102, 103])
        self.assertEqual(self.store.rotation_fingerprint(1), self.rotation.fingerprint)
        end = FIRST + timedelta(weeks=12)
        self.assertEqual(config["next_date"], str(end))
        self.assertEqual(config["next_index"], ((end - LAST).days // 7) % 3)
        self.assertIsNone(self.store.session_on(1, LAST - timedelta(weeks=1)))

    def test_alignment_preserves_notification_and_other_settings(self):
        self.store.configure(
            1,
            1001,
            1002,
            3,
            "09:00",
            "18:00",
            "UTC",
            NOW,
            notifications_enabled=True,
            reminder_start_date=FIRST,
        )
        before = self.store.require_config(1)
        self.align()
        after = self.store.require_config(1)
        for key in before.keys() - {"rotation", "next_date", "next_index"}:
            with self.subTest(key=key):
                self.assertEqual(after[key], before[key])

    def test_repeat_reconfigure_restart_and_file_poll_preserve_alignment(self):
        self.align()
        aligned = self.snapshot()
        self.assertIsNone(apply_rotation(self.store, self.rotation, NOW))
        self.assertEqual(self.align(), (FIRST, 102))
        self.assertEqual(self.snapshot(), aligned)
        self.store.configure(1, 1001, 1002, 3, "09:00", "18:00", "UTC", NOW)
        self.store.close()
        self.store = Store(self.db_path)
        self.assertIsNone(apply_rotation(self.store, self.rotation, NOW + timedelta(weeks=2)))
        self.assertEqual(self.snapshot(), aligned)
        self.store.ensure_horizon(1, TODAY, count=20)
        rows = self.store.upcoming(1, TODAY, 20)
        self.assertEqual(
            [row["presenter_id"] for row in rows],
            [(101, 102, 103)[(index + 1) % 3] for index in range(20)],
        )

    def test_correct_anchor_history_is_retained(self):
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO sessions(guild_id, seminar_date, presenter_id) VALUES(?,?,?)",
                (1, str(LAST), 101),
            )
        anchor = self.store.session_on(1, LAST)
        self.store.mark_sent(anchor["id"], str(LAST), "evening", 9001)
        self.align()
        self.assertEqual(self.store.session_on(1, LAST), anchor)
        self.assertTrue(self.store.was_sent(anchor["id"], str(LAST), "evening"))

    def test_changed_presenter_with_delivery_aborts_entire_transaction(self):
        row = self.store.session_on(1, FIRST)
        self.store.mark_sent(row["id"], str(FIRST), "morning", 9001)
        before = self.snapshot()
        with self.assertRaises(UserError):
            self.align()
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(self.store.session_on(1, LAST))

    def test_changed_presenter_with_completion_receipt_aborts(self):
        row = self.store.session_on(1, FIRST)
        self.store.mark_submission_receipt(row["id"], "example-receipt", 9001)
        before = self.snapshot()
        with self.assertRaises(UserError):
            self.align()
        self.assertEqual(self.snapshot(), before)

    def test_changed_presenter_with_uploaded_document_aborts(self):
        row = self.store.session_on(1, FIRST)
        self.store.add_document(
            1,
            501,
            row["presenter_id"],
            DocumentInfo("Example research paper", "", None, None, "a" * 64, "user_metadata"),
            "paper.pdf",
            "",
            "",
            NOW,
            kind="paper",
            session_id=row["id"],
            source_type="manual",
            upload_key="example-manual-paper",
        )
        before = self.snapshot()
        with self.assertRaises(UserError):
            self.align()
        self.assertEqual(self.snapshot(), before)

    def test_file_change_at_commit_rolls_back_reassignments_and_grants(self):
        row = self.store.session_on(1, FIRST)
        access = SubmissionAccess(self.store)
        invitation, _ = access.issue(1, row["id"], 101, now=100)
        before = self.snapshot()

        def changed():
            raise UserError("작업 중 파일이 변경되었습니다.")

        with self.assertRaises(UserError):
            align_progress(self.store, self.rotation, LAST, 101, TODAY, changed)
        self.assertEqual(self.snapshot(), before)
        grant, _ = access.redeem(invitation, 1, now=101)
        self.assertEqual(access.validate(grant, 1, now=102)["presenter_id"], 101)

    def test_changed_presenter_revokes_old_submission_link(self):
        row = self.store.session_on(1, FIRST)
        access = SubmissionAccess(self.store)
        invitation, _ = access.issue(1, row["id"], 101, now=100)
        self.align()
        with self.assertRaises(SubmissionAccessDenied):
            access.redeem(invitation, 1, now=101)

    def test_holiday_or_swapped_future_is_refused_without_changes(self):
        self.store.postpone(1, TODAY, FIRST)
        before = self.snapshot()
        with self.assertRaises(UserError):
            self.align()
        self.assertEqual(self.snapshot(), before)

    def test_individually_swapped_future_is_refused_without_changes(self):
        self.store.swap(1, FIRST, FIRST + timedelta(weeks=1), TODAY)
        before = self.snapshot()
        with self.assertRaises(UserError):
            self.align()
        self.assertEqual(self.snapshot(), before)

    def prepare_cli(self):
        today = datetime.now(UTC).date()
        first = next_weekday(today, 3)
        last = first - timedelta(weeks=1)
        self.store.set_rotation(1, [101, 102, 103], first, today)
        command = [
            sys.executable,
            "-m",
            "seminar_bot.progress",
            "--last-date",
            str(last),
            "--last-presenter",
            "101",
        ]
        env = dict(
            os.environ,
            DATA_DIR=str(self.root),
            ROTATION_FILE=str(self.rotation_path),
            SEMINAR_GUILD_ID="1",
            DISCORD_GUILD_ID="2",
            PYTHON_DOTENV_DISABLED="1",
        )
        return command, env, first, last

    def test_cli_targets_selected_guild_preserving_stale_guild_and_backup(self):
        command, env, first, last = self.prepare_cli()
        self.store.configure(2, 2001, 2002, 3, "09:00", "18:00", "UTC", NOW)
        self.store.set_rotation(2, [201, 202], FIRST, TODAY)
        stale_config = self.store.require_config(2)
        stale_sessions = self.store.upcoming(2, TODAY, 12)
        active_before = self.store.session_on(1, first)
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.store.session_on(1, last)["presenter_id"], 101)
        self.assertEqual(self.store.session_on(1, first)["presenter_id"], 102)
        self.assertEqual(self.store.session_on(1, first)["id"], active_before["id"])
        self.assertEqual(self.store.require_config(2), stale_config)
        self.assertEqual(self.store.upcoming(2, TODAY, 12), stale_sessions)
        self.assertEqual(self.rotation_path.read_bytes(), self.original_json)
        backups = list((self.root / "backups").glob("before-progress-*.sqlite3"))
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups[0]) as backup:
            self.assertEqual(
                backup.execute(
                    "SELECT presenter_id FROM sessions WHERE guild_id=1 AND seminar_date=?",
                    (str(first),),
                ).fetchone(),
                (101,),
            )
            self.assertIsNone(
                backup.execute(
                    "SELECT id FROM sessions WHERE guild_id=1 AND seminar_date=?", (str(last),)
                ).fetchone()
            )
        if os.name != "nt":
            self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)

    def test_cli_refuses_running_bot_lock_without_mutation(self):
        command, env, _, _ = self.prepare_cli()
        before = self.snapshot()
        with instance_lock(self.root):
            result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(list((self.root / "backups").glob("*.sqlite3")), [])


if __name__ == "__main__":
    unittest.main()

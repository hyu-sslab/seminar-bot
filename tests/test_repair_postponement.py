from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from seminar_bot.__main__ import instance_lock
from seminar_bot.documents import DocumentInfo
from seminar_bot.repair_postponement import run_repair
from seminar_bot.store import Store, UserError
from seminar_bot.submission_access import SubmissionAccess, SubmissionAccessDenied

FIRST = date(2030, 4, 4)
SECOND = FIRST + timedelta(weeks=1)
NOW = datetime(2030, 4, 5, tzinfo=UTC)


class RepairPostponementTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "seminar.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.store.configure(1, 11, 12, FIRST.weekday(), "11:00", "17:00", "UTC", NOW)
        self.store.set_rotation(1, [101, 102, 103], FIRST, FIRST)
        self.store.configure(
            1,
            11,
            12,
            FIRST.weekday(),
            "11:00",
            "17:00",
            "UTC",
            NOW,
            notifications_enabled=True,
            reminder_start_date=FIRST,
        )
        self.store.postpone(1, NOW.date(), SECOND)
        self.session = self.store.session_on(1, FIRST)

    def run_command(self, *, apply=False, presenter_id=101):
        return run_repair(self.path, FIRST, SECOND, presenter_id, apply=apply)

    def rows(self, table):
        return [dict(row) for row in self.store.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def snapshot(self):
        return list(self.store.db.iterdump())

    def test_move_only_the_expected_holiday_and_session_with_links_and_wal_backup(self):
        session_id = self.session["id"]
        self.store.add_document(
            1,
            951,
            101,
            DocumentInfo("Sample paper", "", None, None, "a" * 64, "user_metadata"),
            "paper.pdf",
            "",
            "",
            NOW,
            source_channel_id=12,
            session_id=session_id,
            kind="paper",
            source_type="manual",
            upload_key="test-paper",
        )
        self.store.mark_sent(session_id, str(FIRST), "morning", 750)
        self.store.mark_submission_receipt(session_id, "sample-receipt", 710)
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO holidays(guild_id,seminar_date) VALUES(1,?)",
                (str(SECOND + timedelta(weeks=20)),),
            )
        access = SubmissionAccess(self.store)
        later = self.store.session_on(1, FIRST + timedelta(weeks=4))
        later_invite, _ = access.issue(1, later["id"], 101, scope="presenter", now=10)
        moved_grant, _ = access.redeem(later_invite, 1, now=11)
        peer = self.store.session_on(1, SECOND + timedelta(weeks=1))
        peer_invite, _ = access.issue(1, peer["id"], 102, scope="presenter", now=10)
        peer_grant, _ = access.redeem(peer_invite, 1, now=11)

        before = self.snapshot()
        sessions, holidays = self.rows("sessions"), self.rows("holidays")
        documents, deliveries, receipts, config = (
            self.rows("documents"),
            self.rows("deliveries"),
            self.rows("web_submission_receipts"),
            self.rows("guilds"),
        )
        previews = self.run_command()
        self.assertEqual(previews["status"], "ready")
        self.assertEqual(previews["retained_documents"], 1)
        self.assertEqual(previews["revoked_links"], 1)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.path.parent / "backups").exists())

        result = self.run_command(apply=True)
        self.assertEqual(result["status"], "applied")
        backup = Path(result["backup"])
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(list(db.iterdump()), before)
        self.assertEqual(
            self.rows("sessions"),
            [
                {**row, "seminar_date": str(SECOND)} if row["id"] == session_id else row
                for row in sessions
            ],
        )
        self.assertEqual(
            self.rows("holidays"),
            [
                {**row, "seminar_date": str(FIRST)} if row["seminar_date"] == str(SECOND) else row
                for row in holidays
            ],
        )
        self.assertEqual(self.rows("documents"), documents)
        self.assertEqual(self.rows("deliveries"), deliveries)
        self.assertEqual(self.rows("web_submission_receipts"), receipts)
        self.assertEqual(self.rows("guilds"), [{**config[0], "reminder_start_date": str(SECOND)}])
        with self.assertRaises(SubmissionAccessDenied):
            access.validate(moved_grant, 1, now=12)
        self.assertEqual(access.validate(peer_grant, 1, now=12)["session_id"], peer["id"])
        after = self.snapshot()
        self.assertEqual(self.run_command(apply=True)["status"], "already_correct")
        self.assertEqual(self.snapshot(), after)
        self.assertEqual(list(backup.parent.iterdir()), [backup])

    def test_wrong_presenter_does_not_touch_database(self):
        before = self.snapshot()
        with self.assertRaisesRegex(UserError, "발표자가 지정한 ID와 다릅니다"):
            self.run_command(apply=True, presenter_id=102)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.path.parent / "backups").exists())

    def test_refuses_conflicting_session_and_checks_running_bot_lock(self):
        with instance_lock(self.path.parent), self.assertRaisesRegex(ValueError, "이미 실행 중"):
            self.run_command(apply=True)
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO sessions(guild_id,seminar_date,presenter_id) VALUES(?,?,?)",
                (1, str(SECOND), 103),
            )
        before = self.snapshot()
        with self.assertRaisesRegex(UserError, "예상한 상태와 다릅니다"):
            self.run_command()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.path.parent / "backups").exists())

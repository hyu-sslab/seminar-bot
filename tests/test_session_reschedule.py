from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from seminar_bot.documents import DocumentInfo
from seminar_bot.progress import align_progress
from seminar_bot.rotation import Rotation, apply_rotation
from seminar_bot.scheduling import due_reminders
from seminar_bot.store import Store, UserError
from seminar_bot.submission_access import SubmissionAccess, SubmissionAccessDenied

TODAY = date(2026, 11, 16)
THURSDAY = date(2026, 11, 19)
FRIDAY = THURSDAY + timedelta(days=1)
NOW = datetime(2026, 11, 16, tzinfo=UTC)


class SessionRescheduleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.path = self.root / "seminar.sqlite3"
        self.store = Store(self.path)
        self.store.configure(
            1,
            11,
            12,
            3,
            "11:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            seminar_time="16:00",
            before_minutes=15,
            after_minutes=60,
        )
        self.rotation = Rotation(1, (101, 102, 103), None)
        self.store.set_rotation(
            1,
            list(self.rotation.presenters),
            THURSDAY,
            TODAY,
            fingerprint=self.rotation.fingerprint,
        )
        self.session = self.store.session_on(1, THURSDAY)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def rows(self, table):
        return [dict(row) for row in self.store.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]

    def snapshot(self):
        return list(self.store.db.iterdump())

    def move(self, source=THURSDAY, target=FRIDAY, today=TODAY):
        return self.store.reschedule(1, source, target, today)

    def seed_materials(self):
        data = b"Synthetic existing presentation bytes"
        path = self.root / "files" / "presentation.pdf"
        path.parent.mkdir()
        path.write_bytes(data)
        self.store.add_document(
            1,
            201,
            101,
            DocumentInfo(
                "Existing presentation",
                "Existing presentation body",
                None,
                None,
                hashlib.sha256(data).hexdigest(),
                "pdf_metadata",
            ),
            "presentation.pdf",
            "files/presentation.pdf",
            "",
            NOW,
            source_channel_id=12,
            kind="slides",
            source_type="web",
            session_id=self.session["id"],
            upload_key="synthetic:slides",
        )
        self.store.mark_submission_receipt(self.session["id"], "existing-receipt", 301)
        self.store.mark_sent(self.session["id"], str(THURSDAY), "morning", 302)
        return path, data

    def activate(self, floor=THURSDAY):
        self.store.configure(
            1,
            11,
            12,
            3,
            "11:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            notifications_enabled=True,
            reminder_start_date=floor,
        )

    def test_only_one_row_moves_and_every_existing_material_and_delivery_survives(self):
        path, data = self.seed_materials()
        before_config = self.store.require_config(1)
        before_sessions = self.rows("sessions")
        kept = {
            table: self.rows(table)
            for table in (
                "documents",
                "deliveries",
                "web_submission_receipts",
                "rotation_file_state",
                "holidays",
                "archive_presentations",
                "archive_import_items",
            )
        }
        moved = self.move()
        self.assertEqual(
            moved,
            {
                "id": self.session["id"],
                "presenter_id": 101,
                "seminar_date": str(FRIDAY),
                "original_date": str(THURSDAY),
            },
        )
        self.assertIsNone(self.store.session_on(1, THURSDAY))
        self.assertEqual(self.store.session_on(1, FRIDAY)["id"], self.session["id"])
        after = self.rows("sessions")
        expected = [
            {**row, "seminar_date": str(FRIDAY), "original_date": str(THURSDAY)}
            if row["id"] == self.session["id"]
            else row
            for row in before_sessions
        ]
        self.assertEqual(after, expected)
        self.assertEqual(self.store.require_config(1), before_config)
        for table, rows in kept.items():
            self.assertEqual(self.rows(table), rows)
        self.assertEqual(path.read_bytes(), data)
        self.assertEqual(self.store.submission(1, self.session["id"])["slides"], 1)

    def test_repeated_changes_keep_the_original_anchor_and_return_clears_it(self):
        self.move()
        saturday = FRIDAY + timedelta(days=1)
        moved = self.move(FRIDAY, saturday)
        self.assertEqual(moved["original_date"], str(THURSDAY))
        returned = self.move(saturday, THURSDAY)
        self.assertIsNone(returned["original_date"])
        self.assertEqual(returned["id"], self.session["id"])
        self.assertFalse(self.store.session_has_activity(self.session["id"]))
        self.store.set_rotation(1, [103, 102, 101], THURSDAY, TODAY)
        self.assertEqual(self.store.session_on(1, THURSDAY)["presenter_id"], 103)

    def test_activation_floor_follows_only_the_exact_moved_first_occurrence(self):
        self.activate()
        before = self.store.require_config(1)
        self.move(THURSDAY, TODAY)
        self.assertEqual(
            self.store.require_config(1), {**before, "reminder_start_date": str(TODAY)}
        )
        self.move(TODAY, FRIDAY)
        self.assertEqual(self.store.require_config(1)["reminder_start_date"], str(FRIDAY))
        self.move(FRIDAY, THURSDAY)
        self.assertEqual(self.store.require_config(1), before)
        next_thursday = THURSDAY + timedelta(weeks=1)
        self.move(next_thursday, next_thursday - timedelta(days=2))
        self.assertEqual(self.store.require_config(1), before)

    def test_weekday_change_then_earlier_exception_keeps_the_activation_occurrence(self):
        self.activate()
        self.store.configure(1, 11, 12, 4, "11:00", "17:00", "Asia/Seoul", NOW)
        self.assertEqual(self.store.require_config(1)["reminder_start_date"], str(FRIDAY))
        self.assertEqual(self.store.session(1, self.session["id"])["seminar_date"], str(FRIDAY))
        tuesday = TODAY + timedelta(days=1)
        moved = self.move(FRIDAY, tuesday)
        self.assertEqual(moved["id"], self.session["id"])
        config = self.store.require_config(1)
        self.assertEqual(config["reminder_start_date"], str(tuesday))
        now = datetime.combine(tuesday, datetime.min.time(), UTC).replace(hour=2)
        self.assertEqual(
            [(row["id"], kind) for row, kind in due_reminders(self.store, config, now)],
            [(self.session["id"], "morning")],
        )

    def test_postponement_then_earlier_exception_keeps_the_activation_occurrence(self):
        self.activate()
        self.store.postpone(1, TODAY, THURSDAY)
        postponed = THURSDAY + timedelta(weeks=1)
        self.assertEqual(self.store.require_config(1)["reminder_start_date"], str(postponed))
        tuesday = postponed - timedelta(days=2)
        moved = self.move(postponed, tuesday)
        self.assertEqual(moved["id"], self.session["id"])
        config = self.store.require_config(1)
        self.assertEqual(config["reminder_start_date"], str(tuesday))
        now = datetime.combine(tuesday, datetime.min.time(), UTC).replace(hour=2)
        self.assertEqual(
            [(row["id"], kind) for row, kind in due_reminders(self.store, config, now)],
            [(self.session["id"], "morning")],
        )

    def test_explicit_activation_floor_supplied_with_a_weekday_change_follows_its_session(self):
        self.activate()
        chosen = THURSDAY + timedelta(weeks=1)
        chosen_id = self.store.session_on(1, chosen)["id"]
        self.store.configure(
            1,
            11,
            12,
            4,
            "11:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            notifications_enabled=True,
            reminder_start_date=chosen,
        )
        config = self.store.require_config(1)
        self.assertEqual(config["reminder_start_date"], str(chosen + timedelta(days=1)))
        self.assertEqual(
            self.store.session(1, chosen_id)["seminar_date"], config["reminder_start_date"]
        )

    def test_past_activation_floor_outside_a_shift_stays_unchanged(self):
        self.activate()
        later = datetime(2026, 11, 23, tzinfo=UTC)
        self.store.configure(1, 11, 12, 4, "11:00", "17:00", "Asia/Seoul", later)
        self.assertEqual(self.store.require_config(1)["reminder_start_date"], str(THURSDAY))
        self.store.postpone(1, later.date(), date(2026, 11, 27))
        self.assertEqual(self.store.require_config(1)["reminder_start_date"], str(THURSDAY))

    def test_invalid_dates_conflicts_and_holidays_leave_all_rows_untouched(self):
        self.store.configure(2, 21, 22, 3, "11:00", "17:00", "Asia/Seoul", NOW)
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO sessions(guild_id,seminar_date,presenter_id) VALUES(?,?,?)",
                (1, str(FRIDAY + timedelta(days=1)), 104),
            )
            self.store.db.execute(
                "INSERT INTO holidays(guild_id,seminar_date) VALUES(?,?)",
                (1, str(FRIDAY + timedelta(days=2))),
            )
        before = self.snapshot()
        invalid = [
            (THURSDAY, THURSDAY, TODAY),
            (THURSDAY, TODAY - timedelta(days=1), TODAY),
            (THURSDAY, FRIDAY, FRIDAY + timedelta(days=1)),
            (THURSDAY, THURSDAY + timedelta(weeks=1), TODAY),
            (TODAY, FRIDAY, TODAY),
            (THURSDAY, FRIDAY + timedelta(days=1), TODAY),
            (THURSDAY, FRIDAY + timedelta(days=2), TODAY),
            (str(THURSDAY), FRIDAY, TODAY),
            (THURSDAY, datetime(2026, 11, 20), TODAY),
            (THURSDAY, FRIDAY, True),
        ]
        for arguments in invalid:
            with self.subTest(arguments=arguments), self.assertRaises(UserError):
                self.move(*arguments)
            self.assertEqual(self.snapshot(), before)
        with self.assertRaises(UserError):
            self.store.reschedule(2, THURSDAY, FRIDAY, TODAY)
        self.assertEqual(self.snapshot(), before)

    def test_another_guilds_session_and_holiday_do_not_block_the_destination(self):
        self.store.configure(2, 21, 22, 3, "11:00", "17:00", "Asia/Seoul", NOW)
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO sessions(guild_id,seminar_date,presenter_id) VALUES(?,?,?)",
                (2, str(FRIDAY), 201),
            )
            self.store.db.execute(
                "INSERT INTO holidays(guild_id,seminar_date) VALUES(?,?)", (2, str(FRIDAY))
            )
        before = [row for row in self.rows("sessions") if row["guild_id"] == 2]
        self.move()
        self.assertEqual([row for row in self.rows("sessions") if row["guild_id"] == 2], before)

    def test_this_weeks_past_thursday_can_move_to_today_friday_without_replaying_old_deliveries(
        self,
    ):
        self.activate()
        self.store.mark_sent(self.session["id"], str(THURSDAY), "morning", 401)
        self.store.mark_sent(self.session["id"], str(THURSDAY), "evening", 402)
        old_deliveries = self.rows("deliveries")
        moved = self.move(THURSDAY, FRIDAY, FRIDAY)
        self.assertEqual(moved["original_date"], str(THURSDAY))
        self.assertEqual(self.rows("deliveries"), old_deliveries)
        config = self.store.require_config(1)
        self.assertEqual(config["reminder_start_date"], str(FRIDAY))
        for hour, minute, expected in ((2, 0, "morning"), (6, 45, "before"), (8, 0, "evening")):
            now = datetime.combine(FRIDAY, datetime.min.time(), UTC).replace(
                hour=hour, minute=minute
            )
            due = due_reminders(self.store, config, now)
            self.assertEqual(
                [(row["id"], kind) for row, kind in due], [(self.session["id"], expected)]
            )
            self.store.mark_sent(self.session["id"], str(FRIDAY), expected, 500 + hour)
            self.assertEqual(due_reminders(self.store, config, now), [])
        self.assertTrue(self.store.was_sent(self.session["id"], str(THURSDAY), "morning"))
        self.assertTrue(self.store.was_sent(self.session["id"], str(THURSDAY), "evening"))

    def test_moved_presenter_portals_are_revoked_but_other_presenters_stay_authorized(self):
        access = SubmissionAccess(self.store)
        other_occurrence = self.store.session_on(1, THURSDAY + timedelta(weeks=3))
        invitation, _ = access.issue(1, other_occurrence["id"], 101, now=100, scope="presenter")
        moved_presenter, _ = access.redeem(invitation, 1, now=101)
        other_session = self.store.session_on(1, THURSDAY + timedelta(weeks=1))
        invitation, _ = access.issue(1, other_session["id"], 102, now=102, scope="presenter")
        other_presenter, _ = access.redeem(invitation, 1, now=103)
        pending_session = self.store.session_on(1, THURSDAY + timedelta(weeks=2))
        pending, _ = access.issue(1, pending_session["id"], 103, now=104, scope="presenter")
        self.move()
        with self.assertRaises(SubmissionAccessDenied):
            access.validate(moved_presenter, 1, now=105)
        self.assertEqual(access.validate(other_presenter, 1, now=105)["presenter_id"], 102)
        self.assertEqual(access.redeem(pending, 1, now=105)[1], 105 + 3600)

    def test_old_session_links_never_revive_after_returning_to_the_original_date(self):
        access = SubmissionAccess(self.store)
        invite, _ = access.issue(1, self.session["id"], 101, now=100)
        grant, _ = access.redeem(invite, 1, now=101)
        self.move()
        with self.assertRaises(SubmissionAccessDenied):
            access.validate(grant, 1, now=102)
        self.move(FRIDAY, THURSDAY)
        with self.assertRaises(SubmissionAccessDenied):
            access.validate(grant, 1, now=103)
        with self.assertRaises(SubmissionAccessDenied):
            access.redeem(invite, 1, now=103)

    def test_failed_revocation_rolls_back_date_floor_and_every_access_row(self):
        self.activate()
        access = SubmissionAccess(self.store)
        invitation, _ = access.issue(1, self.session["id"], 101, now=100, scope="presenter")
        access.redeem(invitation, 1, now=101)
        self.store.db.execute(
            "CREATE TRIGGER fail_reschedule BEFORE DELETE ON submission_grants "
            "BEGIN SELECT RAISE(ABORT,'synthetic revocation failure'); END"
        )
        before = self.snapshot()
        with self.assertRaises(sqlite3.IntegrityError):
            self.move()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(self.store.db.in_transaction)

    def test_exception_blocks_rotation_rebuild_even_when_moved_before_rebuild_start(self):
        self.move(THURSDAY, TODAY + timedelta(days=1))
        self.assertTrue(self.store.session_has_activity(self.session["id"]))
        before = self.snapshot()
        with self.assertRaisesRegex(UserError, "개별 날짜 변경"):
            self.store.set_rotation(1, [103, 102, 101], THURSDAY, TODAY)
        self.assertEqual(self.snapshot(), before)
        self.store.set_rotation(1, [103, 102, 101], THURSDAY + timedelta(weeks=1), TODAY)
        self.assertEqual(self.store.session(1, self.session["id"])["original_date"], str(THURSDAY))

    def test_swap_changes_speakers_and_material_ownership_but_keeps_the_date_exception(self):
        self.seed_materials()
        self.move()
        next_day = THURSDAY + timedelta(weeks=1)
        next_session = self.store.session_on(1, next_day)
        self.store.swap(1, FRIDAY, next_day, TODAY)
        self.assertEqual(self.store.session(1, self.session["id"])["original_date"], str(THURSDAY))
        self.assertEqual(self.store.session(1, self.session["id"])["presenter_id"], 102)
        self.assertTrue(self.store.submission(1, next_session["id"])["slides"])
        self.assertTrue(self.store.was_sent(self.session["id"], str(THURSDAY), "morning"))

    def test_postponement_and_weekday_changes_shift_original_and_effective_dates_together(self):
        self.move()
        self.store.postpone(1, TODAY, FRIDAY)
        row = self.store.session(1, self.session["id"])
        self.assertEqual(
            (row["seminar_date"], row["original_date"]),
            (str(FRIDAY + timedelta(weeks=1)), str(THURSDAY + timedelta(weeks=1))),
        )
        self.store.configure(1, 11, 12, 4, "11:00", "17:00", "Asia/Seoul", NOW)
        row = self.store.session(1, self.session["id"])
        self.assertEqual(
            (row["seminar_date"], row["original_date"]),
            (str(FRIDAY + timedelta(days=8)), str(THURSDAY + timedelta(days=8))),
        )

    def test_restart_and_unchanged_rotation_poll_keep_the_exception(self):
        self.move()
        before = self.snapshot()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.snapshot(), before)
        self.assertIsNone(apply_rotation(self.store, self.rotation, NOW))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.store.upcoming(1, TODAY, 1)[0]["seminar_date"], str(FRIDAY))

    def test_progress_alignment_refuses_to_replace_the_exception(self):
        self.move()
        before = self.snapshot()
        with self.assertRaises(UserError):
            align_progress(
                self.store, self.rotation, THURSDAY - timedelta(weeks=1), 103, TODAY, lambda: None
            )
        self.assertEqual(self.snapshot(), before)

    def test_real_schema_eleven_upgrade_preserves_rows_and_adds_null_anchors(self):
        self.seed_materials()
        before_sessions = self.rows("sessions")
        before = {
            table: self.rows(table)
            for table in ("guilds", "documents", "deliveries", "web_submission_receipts")
        }
        self.store.close()
        with sqlite3.connect(self.path) as old:
            old.execute("ALTER TABLE sessions DROP COLUMN original_date")
            old.execute("PRAGMA user_version=11")
        self.store = Store(self.path)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 12)
        self.assertEqual(self.rows("sessions"), before_sessions)
        for table, rows in before.items():
            self.assertEqual(self.rows(table), rows)
        self.assertEqual(self.store.db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()

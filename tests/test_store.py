from __future__ import annotations

import hashlib
import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from seminar_bot.documents import DocumentInfo
from seminar_bot.store import Store, UserError, parse_date, parse_time
from seminar_bot.submission_access import SubmissionAccess

TODAY = date(2026, 10, 15)  # Thursday
NOW = datetime(2026, 10, 15, 1, tzinfo=UTC)  # 10:00 KST


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "seminar.sqlite3"
        self.store = Store(self.path)
        self.configure(1)
        self.store.set_rotation(1, [101, 102, 103], TODAY, TODAY)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def configure(self, guild_id, weekday=3, now=NOW):
        return self.store.configure(
            guild_id, 1001, 1002, weekday, "09:00", "18:00", "Asia/Seoul", now
        )

    def add_document(
        self,
        guild_id=1,
        attachment_id=1,
        user_id=101,
        title="Attention Is All You Need",
        uploaded_at=NOW,
        doi="10.1234/example",
        arxiv="1706.03762",
    ):
        info = DocumentInfo(
            title,
            "content",
            doi,
            arxiv,
            hashlib.sha256(str(attachment_id).encode()).hexdigest(),
            "pdf_metadata",
        )
        return self.store.add_document(
            guild_id,
            attachment_id,
            user_id,
            info,
            "원본_file.pdf",
            "files/a.pdf",
            f"https://discord.com/channels/{guild_id}/2/3",
            uploaded_at,
        )

    def test_rotation_repeats_and_survives_restart(self):
        initial = self.store.upcoming(1, TODAY)
        self.assertEqual(
            [row["presenter_id"] for row in initial], [101, 102, 103, 101, 102, 103, 101, 102]
        )
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.upcoming(1, TODAY), initial)

    def history(self):
        return {
            table: [
                dict(row) for row in self.store.db.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in (
                "sessions",
                "holidays",
                "deliveries",
                "documents",
                "rotation_file_state",
                "web_submission_receipts",
                "web_upload_tombstones",
                "submission_invitations",
                "submission_grants",
            )
        }

    def seed_history_and_submission_access(self):
        record_id, _ = self.add_document()
        self.store.postpone(1, TODAY, TODAY + timedelta(weeks=2))
        session = self.store.session_on(1, TODAY)
        self.store.mark_sent(session["id"], str(TODAY), "morning", 900)
        self.store.mark_submission_receipt(session["id"], "receipt-fingerprint", 901)
        with self.store.db:
            self.store.remember_rotation_file(1, "ab" * 32)
            self.store.db.execute(
                "INSERT INTO web_upload_tombstones VALUES(?,?)", (1, "removed-upload")
            )
        access = SubmissionAccess(self.store)
        invitation, _ = access.issue(1, session["id"], 101, now=1_700_000_000, scope="presenter")
        token, _ = access.redeem(invitation, 1, now=1_700_000_001)
        other = self.store.session_on(1, TODAY + timedelta(weeks=1))
        access.issue(1, other["id"], 102, now=1_700_000_002, scope="presenter")
        return record_id, token

    def test_seminar_time_is_optional_and_omission_or_none_preserves_its_value(self):
        self.assertIsNone(self.store.require_config(1)["seminar_time"])
        self.assertEqual(
            self.store.configure(
                1,
                1001,
                1002,
                3,
                "09:00",
                "18:00",
                "Asia/Seoul",
                NOW,
                seminar_time="14:30",
            ),
            0,
        )
        self.configure(1)
        self.assertEqual(self.store.require_config(1)["seminar_time"], "14:30")
        self.store.configure(
            1, 1001, 1002, 3, "09:00", "18:00", "Asia/Seoul", NOW, seminar_time=None
        )
        self.assertEqual(self.store.require_config(1)["seminar_time"], "14:30")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.require_config(1)["seminar_time"], "14:30")
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 11)

    def test_seminar_time_accepts_midnight_and_last_minute_without_reminder_order_constraints(self):
        for value in ("00:00", "23:59"):
            with self.subTest(value=value):
                self.store.configure(
                    2, 2001, 2002, 3, "09:00", "18:00", "Asia/Seoul", NOW, seminar_time=value
                )
                self.assertEqual(self.store.require_config(2)["seminar_time"], value)

    def test_time_only_update_preserves_dates_materials_deliveries_and_submission_access(self):
        _, token = self.seed_history_and_submission_access()
        before = self.history()
        config = self.store.require_config(1)
        self.assertEqual(
            self.store.configure(
                1,
                1001,
                1002,
                3,
                "09:00",
                "18:00",
                "Asia/Seoul",
                NOW,
                seminar_time="15:00",
            ),
            0,
        )
        self.assertEqual(self.store.require_config(1), {**config, "seminar_time": "15:00"})
        self.assertEqual(self.history(), before)
        self.assertEqual(
            SubmissionAccess(self.store).validate(token, 1, now=1_700_000_003)["presenter_id"], 101
        )

    def test_invalid_seminar_time_cannot_partially_reconfigure_an_existing_or_new_guild(self):
        self.seed_history_and_submission_access()
        self.store.configure(
            1, 1001, 1002, 3, "09:00", "18:00", "Asia/Seoul", NOW, seminar_time="14:30"
        )
        config = self.store.require_config(1)
        before = self.history()
        for value in ("", "9:00", "24:00", "12:60", "14:30:00", True, 1430):
            for guild_id in (1, 2):
                with self.subTest(value=value, guild_id=guild_id), self.assertRaises(UserError):
                    self.store.configure(
                        guild_id, 2001, 2002, 4, "08:00", "19:00", "UTC", NOW, seminar_time=value
                    )
                self.assertEqual(self.store.require_config(1), config)
                self.assertIsNone(self.store.config(2))
                self.assertEqual(self.history(), before)

    def test_real_schema_six_upgrade_preserves_all_records_and_grants(self):
        record_id, token = self.seed_history_and_submission_access()
        config = self.store.require_config(1)
        before = self.history()
        # Remove the actual new column; changing only user_version would not
        # exercise the schema-six migration against an old guilds table.
        with self.store.db:
            self.store.db.execute("ALTER TABLE guilds DROP COLUMN seminar_time")
            self.store.db.execute("PRAGMA user_version=6")
        self.assertNotIn(
            "seminar_time",
            {row["name"] for row in self.store.db.execute("PRAGMA table_info(guilds)")},
        )
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 11)
        column = next(
            row
            for row in self.store.db.execute("PRAGMA table_info(guilds)")
            if row["name"] == "seminar_time"
        )
        self.assertEqual((column["type"], column["notnull"]), ("TEXT", 0))
        self.assertEqual(self.store.require_config(1), config)
        self.assertEqual(self.history(), before)
        self.assertEqual(self.store.document(1, record_id)["file_path"], "files/a.pdf")
        self.assertEqual(
            SubmissionAccess(self.store).validate(token, 1, now=1_700_000_003)["presenter_id"], 101
        )

    def test_future_schema_version_is_rejected_before_creating_or_rewriting_tables(self):
        future = Path(self.temp.name) / "future.sqlite3"
        with sqlite3.connect(future) as db:
            db.execute("CREATE TABLE sentinel(value TEXT)")
            db.execute("INSERT INTO sentinel VALUES('keep')")
            db.execute("PRAGMA user_version=12")
        with self.assertRaisesRegex(ValueError, "버전"):
            Store(future)
        with sqlite3.connect(future) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 12)
            self.assertEqual(db.execute("SELECT * FROM sentinel").fetchall(), [("keep",)])
            self.assertEqual(
                db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                [("sentinel",)],
            )

    def test_schema_seven_upgrade_defaults_notifications_off_and_preserves_history(self):
        _, token = self.seed_history_and_submission_access()
        before = self.history()
        config = self.store.require_config(1)
        with self.store.db:
            self.store.db.execute("ALTER TABLE guilds DROP COLUMN notifications_enabled")
            self.store.db.execute("ALTER TABLE guilds DROP COLUMN reminder_start_date")
            self.store.db.execute("PRAGMA user_version=7")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.require_config(1), config)
        self.assertEqual(self.store.require_config(1)["notifications_enabled"], 0)
        self.assertIsNone(self.store.require_config(1)["reminder_start_date"])
        self.assertEqual(self.history(), before)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 11)
        self.assertEqual(
            SubmissionAccess(self.store).validate(token, 1, now=1_700_000_003)["presenter_id"], 101
        )

    def test_schema_nine_upgrade_preserves_enabled_state_archives_and_submission_access(self):
        _, token = self.seed_history_and_submission_access()
        self.store.configure(
            1,
            1001,
            1002,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            notifications_enabled=True,
            reminder_start_date=TODAY,
        )
        self.store.insert_archive_batch(
            1,
            [
                {
                    "import_key": "fixture:history",
                    "presenter_id": 101,
                    "presenter_name_snapshot": "Historical presenter",
                    "seminar_year": 2026,
                    "seminar_date": None,
                    "documents": [
                        {
                            "import_key": "fixture:paper",
                            "info": DocumentInfo(
                                "Historical research", "", None, None, "", "user_metadata"
                            ),
                            "filename": "historical.pdf",
                            "kind": "paper",
                        }
                    ],
                }
            ],
            imported_at=NOW,
        )
        config = self.store.require_config(1)
        before = self.history()
        archives = {
            table: [tuple(row) for row in self.store.db.execute(f"SELECT * FROM {table}")]
            for table in ("archive_presentations", "archive_import_items")
        }
        with self.store.db:
            self.store.db.execute("ALTER TABLE guilds DROP COLUMN before_reminder")
            self.store.db.execute("ALTER TABLE guilds DROP COLUMN before_minutes")
            self.store.db.execute("ALTER TABLE guilds DROP COLUMN after_minutes")
            self.store.db.execute("PRAGMA user_version=9")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 11)
        self.assertEqual(self.store.require_config(1), config)
        self.assertIsNone(self.store.require_config(1)["before_reminder"])
        self.assertEqual(self.history(), before)
        for table, rows in archives.items():
            self.assertEqual(
                [tuple(row) for row in self.store.db.execute(f"SELECT * FROM {table}")], rows
            )
        self.assertEqual(
            SubmissionAccess(self.store).validate(token, 1, now=1_700_000_003)["presenter_id"],
            101,
        )

    def test_notification_toggle_and_omission_preserve_schedule_progress_and_access(self):
        _, token = self.seed_history_and_submission_access()
        before = self.history()
        original = self.store.require_config(1)
        self.store.configure(
            1,
            1001,
            1002,
            3,
            "09:00",
            "18:00",
            "Asia/Seoul",
            NOW,
            notifications_enabled=True,
            reminder_start_date=TODAY,
        )
        self.configure(1)
        enabled = {**original, "notifications_enabled": 1, "reminder_start_date": str(TODAY)}
        self.assertEqual(self.store.require_config(1), enabled)
        self.store.configure(
            1,
            1001,
            1002,
            3,
            "09:00",
            "18:00",
            "Asia/Seoul",
            NOW,
            notifications_enabled=False,
        )
        self.assertEqual(self.store.require_config(1), {**enabled, "notifications_enabled": 0})
        self.configure(1)
        self.assertEqual(self.store.require_config(1), {**enabled, "notifications_enabled": 0})
        self.store.configure(
            1,
            1001,
            1002,
            3,
            "09:00",
            "18:00",
            "Asia/Seoul",
            NOW,
            notifications_enabled=True,
        )
        self.assertEqual(self.store.require_config(1), enabled)
        self.assertEqual(self.history(), before)
        self.assertEqual(
            SubmissionAccess(self.store).validate(token, 1, now=1_700_000_003)["presenter_id"], 101
        )

    def test_invalid_notification_settings_cannot_partially_change_guild_or_history(self):
        before = self.history()
        config = self.store.require_config(1)
        for kwargs in (
            {"notifications_enabled": True},
            {"notifications_enabled": 1},
            {"notifications_enabled": "on"},
            {"notifications_enabled": True, "reminder_start_date": TODAY + timedelta(days=1)},
            {"reminder_start_date": str(TODAY)},
            {"reminder_start_date": NOW},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(UserError):
                self.store.configure(1, 2001, 2002, 4, "08:00", "19:00", "UTC", NOW, **kwargs)
            self.assertEqual(self.store.require_config(1), config)
            self.assertEqual(self.history(), before)

    def test_swap_preserves_every_sent_slot_to_prevent_same_day_replays(self):
        one = self.store.session_on(1, TODAY)
        for message_id, kind in enumerate(("morning", "before", "evening"), 1):
            self.store.mark_sent(one["id"], str(TODAY), kind, message_id)
        self.store.swap(1, TODAY, TODAY + timedelta(weeks=1), TODAY)
        self.assertEqual(self.store.session_on(1, TODAY)["presenter_id"], 102)
        for kind in ("morning", "before", "evening"):
            self.assertTrue(self.store.was_sent(one["id"], str(TODAY), kind))

    def test_before_reminder_is_opt_in_and_omission_preserves_the_saved_time(self):
        self.assertIsNone(self.store.require_config(1)["before_reminder"])
        self.store.configure(
            1,
            1001,
            1002,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            before_reminder="15:45",
        )
        self.store.configure(1, 1001, 1002, 3, "12:00", "17:00", "Asia/Seoul", NOW)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.require_config(1)["before_reminder"], "15:45")

    def test_three_notification_times_must_be_in_order_without_partial_updates(self):
        self.seed_history_and_submission_access()
        before = self.history()
        original = self.store.require_config(1)
        for value in ("", "3:45", "24:00", "12:00", "11:59", "17:00", "18:00", True, 1545):
            for guild_id in (1, 2):
                with self.subTest(value=value, guild_id=guild_id), self.assertRaises(UserError):
                    self.store.configure(
                        guild_id,
                        1001,
                        1002,
                        3,
                        "12:00",
                        "17:00",
                        "Asia/Seoul",
                        NOW,
                        before_reminder=value,
                    )
                self.assertEqual(self.store.require_config(1), original)
                self.assertEqual(self.history(), before)
                self.assertIsNone(self.store.config(2))
        self.store.configure(
            1,
            1001,
            1002,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            before_reminder="15:45",
        )
        updated = self.store.require_config(1)
        with self.assertRaises(UserError):
            self.store.configure(1, 1001, 1002, 3, "16:00", "17:00", "Asia/Seoul", NOW)
        self.assertEqual(self.store.require_config(1), updated)
        self.assertEqual(self.history(), before)

    def test_notification_times_and_activation_preserve_history_access_and_cursor(self):
        _, token = self.seed_history_and_submission_access()
        original = self.store.require_config(1)
        before = self.history()
        self.store.configure(
            1,
            1001,
            1002,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            before_reminder="15:45",
            notifications_enabled=True,
            reminder_start_date=TODAY,
        )
        self.assertEqual(
            self.store.require_config(1),
            {
                **original,
                "morning": "12:00",
                "evening": "17:00",
                "before_reminder": "15:45",
                "notifications_enabled": 1,
                "reminder_start_date": str(TODAY),
            },
        )
        self.assertEqual(self.history(), before)
        self.assertEqual(
            SubmissionAccess(self.store).validate(token, 1, now=1_700_000_003)["presenter_id"],
            101,
        )

    def test_minute_offsets_follow_start_time_and_keep_all_existing_history(self):
        _, token = self.seed_history_and_submission_access()
        before = self.history()
        cursor = {
            key: self.store.require_config(1)[key]
            for key in ("rotation", "next_date", "next_index")
        }
        self.store.configure(
            1,
            1001,
            1002,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            seminar_time="16:00",
            before_minutes=15,
            after_minutes=60,
            notifications_enabled=True,
            reminder_start_date=TODAY,
        )
        config = self.store.require_config(1)
        self.assertEqual((config["before_reminder"], config["evening"]), ("15:45", "17:00"))
        self.assertEqual((config["before_minutes"], config["after_minutes"]), (15, 60))
        self.store.configure(
            1,
            1001,
            1002,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            seminar_time="15:00",
        )
        config = self.store.require_config(1)
        self.assertEqual((config["before_reminder"], config["evening"]), ("14:45", "16:00"))
        self.assertEqual((config["before_minutes"], config["after_minutes"]), (15, 60))
        self.store.configure(
            1,
            1001,
            1002,
            3,
            "12:00",
            "16:00",
            "Asia/Seoul",
            NOW,
            before_minutes=30,
            after_minutes=90,
        )
        config = self.store.require_config(1)
        self.assertEqual((config["before_reminder"], config["evening"]), ("14:30", "16:30"))
        self.assertEqual((config["before_minutes"], config["after_minutes"]), (30, 90))
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.require_config(1), config)
        self.assertEqual({key: config[key] for key in cursor}, cursor)
        self.assertEqual(self.history(), before)
        self.assertEqual(
            SubmissionAccess(self.store).validate(token, 1, now=1_700_000_003)["presenter_id"],
            101,
        )

    def test_independent_and_zero_minute_offsets_are_supported(self):
        self.store.configure(
            1,
            1001,
            1002,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            seminar_time="16:00",
            before_minutes=0,
        )
        config = self.store.require_config(1)
        self.assertEqual((config["before_reminder"], config["evening"]), ("16:00", "17:00"))
        self.assertIsNone(config["after_minutes"])
        self.store.configure(
            2,
            2001,
            2002,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            seminar_time="16:00",
            before_reminder="15:45",
            after_minutes=0,
        )
        config = self.store.require_config(2)
        self.assertEqual((config["before_reminder"], config["evening"]), ("15:45", "16:00"))
        self.assertIsNone(config["before_minutes"])

    def test_invalid_offsets_report_the_field_without_partial_mutation(self):
        before = self.history()
        config = self.store.require_config(1)
        for field, label in (("before_minutes", "시작전분"), ("after_minutes", "시작후분")):
            for value in (-1, 1440, True, False, "15", 15.0):
                for guild_id in (1, 2):
                    with (
                        self.subTest(field=field, value=value, guild_id=guild_id),
                        self.assertRaisesRegex(UserError, label),
                    ):
                        self.store.configure(
                            guild_id,
                            1001,
                            1002,
                            3,
                            "12:00",
                            "17:00",
                            "Asia/Seoul",
                            NOW,
                            seminar_time="16:00",
                            **{field: value},
                        )
                    self.assertEqual(self.store.require_config(1), config)
                    self.assertEqual(self.history(), before)
                    self.assertIsNone(self.store.config(2))
        for kwargs, label in (
            ({"before_minutes": 15}, "세미나시간"),
            ({"seminar_time": "00:05", "before_minutes": 15}, "시작전분.*전날"),
            ({"seminar_time": "23:45", "after_minutes": 60}, "시작후분.*다음 날"),
            ({"seminar_time": "12:00", "before_minutes": 15}, "시작전분"),
            ({"seminar_time": "16:00", "before_minutes": 0, "after_minutes": 0}, "시작전분"),
        ):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(UserError, label):
                self.store.configure(
                    1, 1001, 1002, 3, "12:00", "17:00", "Asia/Seoul", NOW, **kwargs
                )
            self.assertEqual(self.store.require_config(1), config)
            self.assertEqual(self.history(), before)

    def test_schema_ten_upgrade_keeps_absolute_times_and_disables_offsets(self):
        _, token = self.seed_history_and_submission_access()
        self.store.configure(
            1,
            1001,
            1002,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            seminar_time="16:00",
            before_reminder="15:45",
            notifications_enabled=True,
            reminder_start_date=TODAY,
        )
        config, before = self.store.require_config(1), self.history()
        with self.store.db:
            self.store.db.execute("ALTER TABLE guilds DROP COLUMN before_minutes")
            self.store.db.execute("ALTER TABLE guilds DROP COLUMN after_minutes")
            self.store.db.execute("PRAGMA user_version=10")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 11)
        self.assertEqual(self.store.require_config(1), config)
        self.assertIsNone(self.store.require_config(1)["before_minutes"])
        self.assertIsNone(self.store.require_config(1)["after_minutes"])
        self.store.configure(
            1,
            1001,
            1002,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            seminar_time="15:00",
        )
        updated = self.store.require_config(1)
        self.assertEqual((updated["before_reminder"], updated["evening"]), ("15:45", "17:00"))
        self.assertEqual(self.history(), before)
        self.assertEqual(
            SubmissionAccess(self.store).validate(token, 1, now=1_700_000_003)["presenter_id"],
            101,
        )

    def test_time_input_normalizes_whitespace_and_fullwidth_then_stores_canonical_values(self):
        self.assertEqual(parse_time("　１６：００　").isoformat(), "16:00:00")
        self.assertEqual(parse_time(" 12:00 ").isoformat(), "12:00:00")
        self.store.configure(
            1,
            1001,
            1002,
            3,
            " １２：００ ",
            "１７：００",
            "Asia/Seoul",
            NOW,
            seminar_time="　１６：００　",
            before_reminder="１５：４５",
        )
        config = self.store.require_config(1)
        self.assertEqual(
            (
                config["morning"],
                config["seminar_time"],
                config["before_reminder"],
                config["evening"],
            ),
            ("12:00", "16:00", "15:45", "17:00"),
        )
        for field, label in (
            ("seminar_time", "세미나시간"),
            ("morning", "일정안내"),
            ("before_reminder", "시작 전 알림 시각"),
            ("evening", "시작 후 알림 시각"),
        ):
            for value in ("bad", "16:00:00", "16시", "4:00", True):
                arguments = {
                    "morning": "12:00",
                    "evening": "17:00",
                    "seminar_time": "16:00",
                    "before_reminder": "15:45",
                    field: value,
                }
                with (
                    self.subTest(field=field, value=value),
                    self.assertRaisesRegex(UserError, label),
                ):
                    self.store.configure(
                        1,
                        1001,
                        1002,
                        3,
                        arguments.pop("morning"),
                        arguments.pop("evening"),
                        "Asia/Seoul",
                        NOW,
                        **arguments,
                    )
                self.assertEqual(self.store.require_config(1), config)

    def test_postpone_moves_entire_schedule_and_generation_cursor(self):
        before = self.store.upcoming(1, TODAY, 20)
        old_cursor = date.fromisoformat(self.store.config(1)["next_date"])
        start, count = self.store.postpone(1, TODAY)
        self.assertEqual((start, count), (TODAY, 20))
        self.assertIsNone(self.store.session_on(1, TODAY))
        after = self.store.upcoming(1, TODAY, 20)
        for old, new in zip(before, after, strict=True):
            self.assertEqual(new["presenter_id"], old["presenter_id"])
            self.assertEqual(
                date.fromisoformat(new["seminar_date"]),
                date.fromisoformat(old["seminar_date"]) + timedelta(days=7),
            )
        self.assertEqual(
            date.fromisoformat(self.store.config(1)["next_date"]), old_cursor + timedelta(days=7)
        )
        far_future = TODAY + timedelta(weeks=19)
        extended = self.store.upcoming(1, far_future, 8)
        self.assertEqual(extended[2]["seminar_date"], (TODAY + timedelta(weeks=21)).isoformat())
        self.assertEqual(extended[2]["presenter_id"], 103)  # original cycle index20

    def test_postpone_from_later_date_keeps_earlier_schedule_and_history(self):
        self.store.mark_sent(
            self.store.session_on(1, TODAY)["id"], TODAY.isoformat(), "morning", 900
        )
        record_id, _ = self.add_document()
        original = self.store.document(1, record_id)
        self.store.postpone(1, TODAY, TODAY + timedelta(weeks=2))
        self.assertEqual(self.store.document(1, record_id), original)
        self.assertEqual(self.store.session_on(1, TODAY + timedelta(weeks=1))["presenter_id"], 102)
        self.assertIsNone(self.store.session_on(1, TODAY + timedelta(weeks=2)))

    def test_repeated_holidays_preserve_order(self):
        self.store.postpone(1, TODAY)
        self.store.postpone(1, TODAY)
        self.assertEqual(
            [r["seminar_date"] for r in self.store.holidays(1, TODAY)], ["2026-10-15", "2026-10-22"]
        )
        self.assertEqual(self.store.upcoming(1, TODAY)[0]["seminar_date"], "2026-10-29")

    def test_past_mutations_rejected(self):
        with self.assertRaises(UserError):
            self.store.postpone(1, TODAY + timedelta(days=1), TODAY)
        with self.assertRaises(UserError):
            self.store.swap(1, TODAY, TODAY + timedelta(weeks=1), TODAY + timedelta(days=1))

    def test_swap_moves_preuploaded_materials_with_speaker(self):
        record_id, _ = self.add_document()
        first = self.store.session_on(1, TODAY)
        self.store.swap(1, TODAY, TODAY + timedelta(weeks=1), TODAY)
        self.assertEqual(self.store.session_on(1, TODAY)["presenter_id"], 102)
        material = self.store.document(1, record_id)
        self.assertEqual((material["presenter_id"], material["seminar_date"]), (101, "2026-10-22"))
        self.assertEqual(self.store.session_on(1, TODAY + timedelta(weeks=3))["presenter_id"], 101)
        self.assertEqual(first["id"], self.store.session_on(1, TODAY)["id"])

    def test_weekday_change_preserves_cycle_and_holiday(self):
        self.store.postpone(1, TODAY)
        self.assertEqual(self.configure(1, weekday=4), 1)
        self.assertEqual(self.store.upcoming(1, TODAY)[0]["seminar_date"], "2026-10-23")
        self.assertEqual(self.store.holidays(1, TODAY)[0]["seminar_date"], "2026-10-16")
        self.assertEqual(date.fromisoformat(self.store.config(1)["next_date"]).weekday(), 4)

    def test_weekday_change_after_long_downtime_updates_stale_cursor(self):
        later = datetime(2028, 1, 1, tzinfo=UTC)
        self.configure(1, weekday=1, now=later)
        upcoming = self.store.upcoming(1, later.date())
        self.assertTrue(
            all(date.fromisoformat(row["seminar_date"]).weekday() == 1 for row in upcoming)
        )

    def test_downtime_advances_rotation_without_replaying_old_dates(self):
        later = TODAY + timedelta(weeks=40)
        rows = self.store.upcoming(1, later)
        self.assertEqual(rows[0]["seminar_date"], later.isoformat())
        self.assertEqual(rows[0]["presenter_id"], [101, 102, 103][40 % 3])

    def test_rotation_reset_protects_materials_and_sent_sessions(self):
        self.add_document()
        with self.assertRaises(UserError):
            self.store.set_rotation(1, [104], TODAY, TODAY)
        self.store.set_rotation(1, [104], TODAY + timedelta(weeks=1), TODAY)
        self.assertEqual(self.store.session_on(1, TODAY)["presenter_id"], 101)
        self.assertEqual(self.store.session_on(1, TODAY + timedelta(weeks=1))["presenter_id"], 104)

    def test_rotation_validation(self):
        for presenters, start in [
            ([], TODAY),
            ([1, 1], TODAY),
            ([1], TODAY + timedelta(days=1)),
            ([1], TODAY - timedelta(weeks=1)),
        ]:
            with self.subTest(presenters=presenters, start=start), self.assertRaises(UserError):
                self.store.set_rotation(1, presenters, start, TODAY)

    def test_config_and_datetime_validation(self):
        for morning, evening, timezone in [
            ("9:00", "18:00", "UTC"),
            ("19:00", "18:00", "UTC"),
            ("09:00", "18:00", "invalid/zone"),
        ]:
            with self.subTest(timezone=timezone, morning=morning), self.assertRaises(UserError):
                self.store.configure(2, 20, 21, 3, morning, evening, timezone)
        for invalid in ("2026-2-01", "2026-02-30", "yesterday"):
            with self.assertRaises(UserError):
                parse_date(invalid)
        with self.assertRaises(UserError):
            parse_time("25:00")

    def test_auto_link_uses_uploader_date_in_guild_timezone(self):
        # 23:30 UTC Wednesday is Thursday in Seoul.
        record_id, _ = self.add_document(uploaded_at=datetime(2026, 10, 14, 23, 30, tzinfo=UTC))
        self.assertEqual(self.store.document(1, record_id)["seminar_date"], "2026-10-15")
        other_id, _ = self.add_document(attachment_id=2, user_id=999)
        self.assertIsNone(self.store.document(1, other_id)["session_id"])

    def test_nearest_session_tie_prefers_previous_seminar(self):
        self.store.set_rotation(1, [101], TODAY, TODAY)
        midpoint = TODAY + timedelta(days=3)
        self.assertEqual(
            self.store.guess_session(1, 101, midpoint), self.store.session_on(1, TODAY)["id"]
        )
        self.assertIsNone(self.store.guess_session(1, 999, midpoint))

    def test_search_unicode_identifiers_urls_and_literal_sql(self):
        self.add_document(title="한글 논문: Attention Is All You Need")
        queries = [
            "한글논문",
            "attention-is",
            "ＡＴＴＥＮＴＩＯＮ",
            "원본 file",
            "10.1234/example",
            "https://doi.org/10.1234/example",
            "DOI: 10.1234/example",
            "arXiv:1706.03762v5",
            "https://arxiv.org/pdf/1706.03762v2.pdf",
        ]
        for query in queries:
            with self.subTest(query=query):
                self.assertEqual(self.store.search(1, query)[1], 1)
        self.assertEqual(self.store.search(1, "' OR 1=1 --")[1], 0)
        with self.assertRaises(UserError):
            self.store.search(1, "%%%")

    def test_duplicate_ids_and_titles_detected_but_records_preserved(self):
        first, _ = self.add_document()
        second, matches = self.add_document(attachment_id=2)
        self.assertEqual(matches[0]["id"], first)
        self.assertNotEqual(first, second)
        _, title_matches = self.add_document(attachment_id=3, doi=None, arxiv=None)
        self.assertTrue(title_matches)
        self.assertTrue(self.store.has_attachment(1, 2))

    def test_server_isolation_in_search_duplicates_and_edits(self):
        first, _ = self.add_document()
        self.configure(2)
        self.assertEqual(self.store.search(2, "attention")[1], 0)
        with self.assertRaises(UserError):
            self.store.document(2, first)
        _, matches = self.add_document(guild_id=2)
        self.assertEqual(matches, [])

    def test_only_uploader_or_manager_can_edit_and_link(self):
        record_id, _ = self.add_document()
        with self.assertRaises(UserError):
            self.store.rename_document(1, record_id, "new", 999, False)
        self.store.rename_document(1, record_id, "새로운 논문 제목", 101, False)
        self.assertEqual(self.store.search(1, "새로운논문")[1], 1)
        with self.assertRaises(UserError):
            self.store.link_document(1, record_id, TODAY + timedelta(weeks=1), 101, False)
        self.store.link_document(1, record_id, TODAY + timedelta(weeks=1), 999, True)
        self.assertEqual(self.store.document(1, record_id)["presenter_id"], 102)

    def test_search_pagination(self):
        for attachment_id in range(1, 8):
            self.add_document(attachment_id=attachment_id)
        first, count = self.store.search(1, "attention")
        second, _ = self.store.search(1, "attention", page=2)
        self.assertEqual((len(first), len(second), count), (5, 2, 7))
        self.assertGreater(first[0]["id"], second[0]["id"])

    def test_search_channel_access_and_pagination_are_filtered_together(self):
        self.add_document()
        self.assertEqual(self.store.search(1, "attention", visible_channel_ids=[])[1], 0)
        self.assertEqual(self.store.search(1, "attention", visible_channel_ids=[999])[1], 0)
        self.assertEqual(self.store.search(1, "attention", visible_channel_ids=[1002])[1], 1)

    def test_search_does_not_strip_identifier_letters_from_title_words(self):
        self.add_document(title="Doing Experiments With Arxiv Papers")
        self.assertEqual(self.store.search(1, "doing")[1], 1)
        self.assertEqual(self.store.search(1, "arxiv papers")[1], 1)


if __name__ == "__main__":
    unittest.main()

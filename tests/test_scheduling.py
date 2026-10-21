from __future__ import annotations

import hashlib
import unittest
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from seminar_bot.documents import DocumentInfo
from seminar_bot.scheduling import due_reminders
from seminar_bot.store import Store


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.day = date(2026, 10, 15)
        self.store.configure(1, 2, 3, 3, "09:00", "18:00", "Asia/Seoul")
        self.store.set_rotation(1, [101, 102], self.day, self.day)
        self.store.configure(
            1,
            2,
            3,
            3,
            "09:00",
            "18:00",
            "Asia/Seoul",
            notifications_enabled=True,
            reminder_start_date=self.day,
        )
        self.config = self.store.config(1)
        self.attachment_id = 1000

    def tearDown(self):
        self.store.close()

    def at(self, hour, minute=0, day=15):
        return due_reminders(
            self.store, self.config, datetime(2026, 10, day, hour, minute, tzinfo=UTC)
        )

    def snapshot(self):
        return "\n".join(self.store.db.iterdump())

    def complete(self, on_date, channel_id=3):
        session = self.store.session_on(1, on_date)
        for index in range(4):
            self.attachment_id += 1
            kind = "slides" if index == 0 else "paper"
            title = f"Synthetic {kind} reference {self.attachment_id}"
            source = "web" if kind == "slides" else "manual"
            self.store.add_document(
                1,
                self.attachment_id,
                session["presenter_id"],
                DocumentInfo(
                    title,
                    "",
                    f"10.9999/synthetic-{self.attachment_id}" if kind == "paper" else None,
                    None,
                    hashlib.sha256(str(self.attachment_id).encode()).hexdigest(),
                    "user_metadata" if source == "manual" else "pdf_metadata",
                ),
                f"synthetic-{self.attachment_id}.pdf",
                "files/synthetic.pdf" if kind == "slides" else "",
                "",
                datetime(2026, 10, 15, tzinfo=UTC),
                source_channel_id=channel_id,
                kind=kind,
                session_id=session["id"],
                source_type=source,
                upload_key=f"synthetic-{self.attachment_id}",
            )
        return session

    def test_seoul_morning_and_evening_boundaries(self):
        self.assertEqual(self.at(23, 59, day=14), [])
        self.assertEqual(self.at(0)[0][1], "morning")
        self.assertEqual(self.at(8, 59)[0][1], "morning")
        self.assertEqual(self.at(9)[0][1], "evening")

    def test_disabled_missing_floor_and_future_floor_do_not_mutate_database(self):
        before = self.snapshot()
        for overrides in (
            {"notifications_enabled": False},
            {"notifications_enabled": 0},
            {"reminder_start_date": None},
            {"reminder_start_date": "2026-10-22"},
        ):
            with self.subTest(overrides=overrides):
                self.assertEqual(
                    due_reminders(
                        self.store,
                        {**self.config, **overrides},
                        datetime(2026, 10, 15, 9, tzinfo=UTC),
                    ),
                    [],
                )
                self.assertEqual(self.snapshot(), before)

    def test_a_new_guild_defaults_to_notifications_off(self):
        self.store.configure(2, 4, 5, 3, "09:00", "18:00", "Asia/Seoul")
        config = self.store.config(2)
        self.assertFalse(config["notifications_enabled"])
        self.assertIsNone(config["reminder_start_date"])
        before = self.snapshot()
        self.assertEqual(
            due_reminders(self.store, config, datetime(2030, 10, 15, 9, tzinfo=UTC)), []
        )
        self.assertEqual(self.snapshot(), before)

    def test_missing_today_row_is_not_recreated(self):
        with self.store.db:
            self.store.db.execute(
                "DELETE FROM sessions WHERE guild_id=? AND seminar_date=?", (1, str(self.day))
            )
        before = self.snapshot()
        with patch.object(self.store, "ensure_horizon", side_effect=AssertionError("generated")):
            self.assertEqual(self.at(0), [])
            self.assertEqual(self.at(9), [])
        self.assertEqual(self.snapshot(), before)

    def test_long_downtime_does_not_generate_sessions_or_modify_the_cursor(self):
        with self.store.db:
            self.store.db.execute("DELETE FROM sessions WHERE guild_id=1")
        before = self.snapshot()
        self.assertEqual(
            due_reminders(self.store, self.config, datetime(2030, 10, 17, 9, tzinfo=UTC)), []
        )
        self.assertEqual(self.snapshot(), before)

    def test_delivery_log_suppresses_morning_restart_replay(self):
        session, kind = self.at(0)[0]
        self.store.mark_sent(session["id"], str(self.day), kind, 123)
        self.assertEqual(self.at(1), [])
        self.assertEqual(self.at(9)[0][1], "evening")

    def test_morning_is_for_today_only_and_requires_the_configured_weekday(self):
        self.assertEqual(self.at(0, day=16), [])
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO sessions(guild_id, seminar_date, presenter_id) VALUES(?,?,?)",
                (1, "2026-10-16", 103),
            )
        self.assertEqual(self.at(0, day=16), [])
        self.assertEqual(self.at(15), [])  # Midnight on the following local day.

    def test_morning_announcement_still_sends_when_today_is_complete(self):
        self.complete(self.day)
        self.assertEqual(self.at(0)[0][1], "morning")
        self.assertEqual(self.at(9), [])

    def test_evening_can_request_an_existing_past_session_on_a_non_seminar_day(self):
        session, kind = self.at(9, day=16)[0]
        self.assertEqual((session["seminar_date"], kind), (str(self.day), "evening"))
        before = self.snapshot()
        self.assertEqual(self.at(9, day=16)[0][0]["id"], session["id"])
        self.assertEqual(self.snapshot(), before)

    def test_evening_selects_the_oldest_incomplete_session(self):
        session, kind = self.at(9, day=29)[0]
        self.assertEqual((session["seminar_date"], kind), ("2026-10-15", "evening"))
        self.complete(self.day)
        self.assertEqual(self.at(9, day=29)[0][0]["seminar_date"], "2026-10-22")

    def test_a_past_record_from_an_older_weekday_setting_remains_eligible(self):
        self.complete(self.day)
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO sessions(guild_id, seminar_date, presenter_id) VALUES(?,?,?)",
                (1, "2026-10-16", 103),
            )
        self.assertEqual(self.at(9, day=22)[0][0]["seminar_date"], "2026-10-16")

    def test_completed_sessions_are_skipped_and_future_sessions_are_not_requested(self):
        self.complete(self.day)
        self.complete(self.day + timedelta(weeks=1))
        self.assertEqual(self.at(9, day=22), [])

    def test_completion_uses_only_the_configured_upload_scope(self):
        session = self.complete(self.day, channel_id=999)
        self.assertTrue(self.store.submission(1, session["id"])["complete"])
        self.assertFalse(
            self.store.submission(1, session["id"], visible_channel_ids=[3])["complete"]
        )
        self.assertEqual(self.at(9)[0][0]["id"], session["id"])

    def test_a_past_evening_delivery_suppresses_that_session_for_its_lifetime(self):
        session = self.store.session_on(1, self.day)
        self.store.mark_sent(session["id"], "2026-10-16", "evening", 123)
        self.assertEqual(self.at(9, day=29)[0][0]["seminar_date"], "2026-10-22")

    def test_backlog_is_bounded_to_one_evening_per_local_day_and_one_per_session(self):
        for on_date, expected in (
            ("2026-10-29", "2026-10-15"),
            ("2026-10-30", "2026-10-22"),
            ("2026-10-31", "2026-10-29"),
        ):
            now = datetime.fromisoformat(on_date + "T09:00:00+00:00")
            due = due_reminders(self.store, self.config, now)
            self.assertEqual(len(due), 1)
            session, kind = due[0]
            self.assertEqual(session["seminar_date"], expected)
            self.store.mark_sent(session["id"], on_date, kind, 123)
            self.assertEqual(due_reminders(self.store, self.config, now), [])

    def test_daily_cap_applies_even_if_the_sent_session_precedes_the_current_floor(self):
        with self.store.db:
            cursor = self.store.db.execute(
                "INSERT INTO sessions(guild_id, seminar_date, presenter_id) VALUES(?,?,?)",
                (1, "2026-10-08", 103),
            )
        self.store.mark_sent(cursor.lastrowid, str(self.day), "evening", 123)
        self.assertEqual(self.at(9), [])

    def test_sessions_before_the_floor_and_other_guild_deliveries_are_excluded(self):
        self.store.configure(2, 4, 5, 3, "09:00", "18:00", "Asia/Seoul")
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO sessions(guild_id, seminar_date, presenter_id) VALUES(?,?,?)",
                (1, "2026-10-08", 103),
            )
            cursor = self.store.db.execute(
                "INSERT INTO sessions(guild_id, seminar_date, presenter_id) VALUES(?,?,?)",
                (2, str(self.day), 201),
            )
        self.store.mark_sent(cursor.lastrowid, str(self.day), "evening", 123)
        self.assertEqual(self.at(9)[0][0]["seminar_date"], str(self.day))

    def test_holidays_are_excluded_even_when_a_stale_session_row_exists(self):
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO holidays(guild_id, seminar_date) VALUES(?,?)", (1, str(self.day))
            )
        self.assertEqual(self.at(0), [])
        self.assertEqual(self.at(9), [])
        self.assertEqual(self.at(9, day=29)[0][0]["seminar_date"], "2026-10-22")

    def test_postponement_cancels_today_and_next_week_reminds_again(self):
        session, kind = self.at(0)[0]
        self.store.mark_sent(session["id"], str(self.day), kind, 123)
        self.store.postpone(1, self.day)
        self.assertEqual(self.at(9), [])
        next_due = due_reminders(self.store, self.config, datetime(2026, 10, 22, 0, tzinfo=UTC))
        self.assertEqual(next_due[0][0]["presenter_id"], 101)
        self.assertEqual(next_due[0][1], "morning")

    def test_swap_after_morning_notifies_new_speaker_but_retains_the_evening_cap(self):
        session, kind = self.at(0)[0]
        self.store.mark_sent(session["id"], str(self.day), kind, 123)
        self.store.swap(1, self.day, self.day + timedelta(weeks=1), self.day)
        self.assertEqual(self.at(1)[0][0]["presenter_id"], 102)
        self.store.mark_sent(session["id"], str(self.day), "evening", 124)
        self.store.swap(1, self.day, self.day + timedelta(weeks=1), self.day)
        self.assertEqual(self.at(9), [])

    def test_dst_fall_back_local_morning_does_not_duplicate(self):
        day = date(2026, 11, 1)
        self.store.configure(2, 4, 5, 6, "01:15", "20:00", "America/New_York")
        self.store.set_rotation(2, [201], day, day)
        self.store.configure(
            2,
            4,
            5,
            6,
            "01:15",
            "20:00",
            "America/New_York",
            notifications_enabled=True,
            reminder_start_date=day,
        )
        config = self.store.config(2)
        due = due_reminders(self.store, config, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
        session, kind = due[0]
        self.store.mark_sent(session["id"], str(day), kind, 124)
        self.assertEqual(
            due_reminders(self.store, config, datetime(2026, 11, 1, 6, 30, tzinfo=UTC)), []
        )

    def test_evening_daily_cap_uses_the_guild_local_date_instead_of_the_utc_date(self):
        floor = date(2026, 10, 25)
        self.store.configure(2, 4, 5, 6, "01:15", "20:00", "America/New_York")
        self.store.set_rotation(2, [201, 202], floor, floor)
        self.store.configure(
            2,
            4,
            5,
            6,
            "01:15",
            "20:00",
            "America/New_York",
            notifications_enabled=True,
            reminder_start_date=floor,
        )
        config = self.store.config(2)
        due = due_reminders(self.store, config, datetime(2026, 11, 2, 1, 30, tzinfo=UTC))
        session, kind = due[0]
        self.assertEqual(session["seminar_date"], "2026-10-25")
        self.store.mark_sent(session["id"], "2026-11-01", kind, 124)
        self.assertEqual(
            due_reminders(self.store, config, datetime(2026, 11, 2, 2, 30, tzinfo=UTC)), []
        )
        next_day = due_reminders(self.store, config, datetime(2026, 11, 3, 1, 30, tzinfo=UTC))
        self.assertEqual(next_day[0][0]["seminar_date"], "2026-11-01")


if __name__ == "__main__":
    unittest.main()

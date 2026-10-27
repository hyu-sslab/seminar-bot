from __future__ import annotations

import hashlib
import unittest
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from seminar_bot.documents import DocumentInfo
from seminar_bot.scheduling import due_reminders
from seminar_bot.store import Store

DAY = date(2026, 10, 29)
NOW = datetime(2026, 10, 27, tzinfo=UTC)


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.day = DAY
        self.store.configure(
            1,
            2,
            3,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            seminar_time="16:00",
            before_reminder="15:45",
        )
        # Keep old recorded sessions deliberately. Activating this week's
        # reminders must not send requests for any of those earlier talks.
        self.store.set_rotation(1, [101, 102, 103], date(2026, 10, 15), date(2026, 10, 15))
        self.store.configure(
            1,
            2,
            3,
            3,
            "12:00",
            "17:00",
            "Asia/Seoul",
            NOW,
            notifications_enabled=True,
            reminder_start_date=DAY,
        )
        self.config = self.store.config(1)
        self.attachment_id = 1000

    def tearDown(self):
        self.store.close()

    def at(self, hour, minute=0, *, day=DAY, config=None):
        config = self.config if config is None else config
        current = datetime.combine(day, time(hour, minute), ZoneInfo(config["timezone"]))
        return due_reminders(self.store, config, current.astimezone(UTC))

    def snapshot(self):
        return "\n".join(self.store.db.iterdump())

    def add_material(self, on_date, *, kind="paper", channel_id=3, identity=None):
        session = self.store.session_on(1, on_date)
        self.attachment_id += 1
        identity = str(self.attachment_id) if identity is None else identity
        source = "web" if kind == "slides" else "manual"
        record_id, _ = self.store.add_document(
            1,
            self.attachment_id,
            session["presenter_id"],
            DocumentInfo(
                f"Synthetic {kind} reference {identity}",
                "",
                f"10.9999/synthetic-{identity}" if kind == "paper" else None,
                None,
                hashlib.sha256(identity.encode()).hexdigest(),
                "user_metadata" if source == "manual" else "pdf_metadata",
            ),
            f"synthetic-{self.attachment_id}.pdf",
            "files/synthetic.pdf" if kind == "slides" else "",
            "",
            NOW,
            source_channel_id=channel_id,
            kind=kind,
            session_id=session["id"],
            source_type=source,
            upload_key=f"synthetic-{self.attachment_id}",
        )
        return record_id

    def complete(self, on_date=DAY, channel_id=3):
        self.add_material(on_date, kind="slides", channel_id=channel_id)
        for _ in range(3):
            self.add_material(on_date, channel_id=channel_id)
        return self.store.session_on(1, on_date)

    def test_seoul_three_stage_boundaries_are_expressed_in_utc(self):
        for hour, minute, expected in (
            (2, 59, None),
            (3, 0, "morning"),
            (6, 44, "morning"),
            (6, 45, "before"),
            (7, 59, "before"),
            (8, 0, "evening"),
            (14, 59, "evening"),
            (15, 0, None),
        ):
            with self.subTest(utc=(hour, minute)):
                due = due_reminders(
                    self.store, self.config, datetime(2026, 10, 29, hour, minute, tzinfo=UTC)
                )
                self.assertEqual([kind for _, kind in due], [] if expected is None else [expected])

    def test_each_stage_is_delivered_at_most_once_per_session_and_local_day(self):
        for hour, minute, expected in ((12, 0, "morning"), (15, 45, "before"), (17, 0, "evening")):
            with self.subTest(stage=expected):
                session, kind = self.at(hour, minute)[0]
                self.assertEqual(kind, expected)
                self.store.mark_sent(session["id"], str(DAY), kind, 123)
                self.assertEqual(self.at(hour, minute), [])
                self.assertEqual(self.at(hour, minute + 1), [])
        self.assertEqual(self.at(23, 59), [])

    def test_late_restart_selects_only_the_current_stage_without_a_catch_up_burst(self):
        self.assertEqual([kind for _, kind in self.at(16)], ["before"])
        session, kind = self.at(18)[0]
        self.assertEqual(kind, "evening")
        self.store.mark_sent(session["id"], str(DAY), kind, 123)
        self.assertEqual(self.at(18), [])
        self.assertEqual(self.at(23, 59), [])
        self.assertEqual(self.at(17, day=DAY + timedelta(days=1)), [])
        self.assertEqual(self.at(17, day=DAY + timedelta(days=2)), [])

    def test_disabled_missing_floor_and_future_floor_do_not_mutate_database(self):
        before = self.snapshot()
        for overrides in (
            {"notifications_enabled": False},
            {"notifications_enabled": 0},
            {"reminder_start_date": None},
            {"reminder_start_date": "2026-11-05"},
        ):
            for hour in (12, 16, 17):
                with self.subTest(overrides=overrides, hour=hour):
                    self.assertEqual(self.at(hour, config={**self.config, **overrides}), [])
                    self.assertEqual(self.snapshot(), before)

    def test_a_new_guild_defaults_to_notifications_off(self):
        self.store.configure(2, 4, 5, 3, "12:00", "17:00", "Asia/Seoul", NOW)
        config = self.store.config(2)
        self.assertFalse(config["notifications_enabled"])
        self.assertIsNone(config["reminder_start_date"])
        before = self.snapshot()
        self.assertEqual(self.at(17, config=config), [])
        self.assertEqual(self.snapshot(), before)

    def test_activation_on_october_29_excludes_every_earlier_recorded_talk(self):
        for day in (date(2026, 10, 15), date(2026, 10, 22)):
            self.assertIsNotNone(self.store.session_on(1, day))
            for hour in (12, 16, 17):
                self.assertEqual(self.at(hour, day=day), [])
        for hour in (12, 16, 17):
            due = self.at(hour)
            self.assertEqual(len(due), 1)
            self.assertEqual(due[0][0]["seminar_date"], "2026-10-29")

    def test_evening_never_walks_the_backlog_or_requests_future_sessions(self):
        for day in (
            DAY - timedelta(weeks=2),
            DAY - timedelta(weeks=1),
            DAY,
            DAY + timedelta(weeks=1),
        ):
            self.assertIsNotNone(self.store.session_on(1, day))
        self.assertEqual(self.at(17)[0][0]["seminar_date"], str(DAY))
        self.complete()
        self.assertEqual(self.at(17), [])
        self.assertEqual(self.at(17, day=DAY + timedelta(days=1)), [])
        self.assertEqual(
            self.at(17, day=DAY + timedelta(weeks=1))[0][0]["seminar_date"], "2026-11-05"
        )

    def test_missing_today_row_is_never_recreated(self):
        with self.store.db:
            self.store.db.execute(
                "DELETE FROM sessions WHERE guild_id=? AND seminar_date=?", (1, str(DAY))
            )
        before = self.snapshot()
        with patch.object(self.store, "ensure_horizon", side_effect=AssertionError("generated")):
            for hour in (12, 16, 17):
                self.assertEqual(self.at(hour), [])
        self.assertEqual(self.snapshot(), before)

    def test_unknown_and_dated_imported_history_are_not_operational_reminder_sessions(self):
        groups = []
        for index, actual_day in enumerate((None, DAY)):
            groups.append(
                {
                    "import_key": f"sample:archive:{index}",
                    "presenter_id": 101,
                    "presenter_name_snapshot": "Historical presenter",
                    "seminar_year": 2026,
                    "seminar_date": actual_day,
                    "archive_kind": "presentation",
                    "documents": [
                        {
                            "import_key": f"sample:archive-paper:{index}",
                            "kind": "paper",
                            "filename": "past-paper.pdf",
                            "info": DocumentInfo(
                                "Historical research", "", None, None, "", "user_metadata"
                            ),
                        }
                    ],
                }
            )
        self.store.insert_archive_batch(
            1, groups, imported_at=datetime(2026, 10, 29, 12, tzinfo=UTC)
        )
        # Even a same-day imported archive cannot replace a missing real session.
        with self.store.db:
            self.store.db.execute(
                "DELETE FROM sessions WHERE guild_id=1 AND seminar_date=?", (str(DAY),)
            )
        before = self.snapshot()
        for hour in (12, 16, 17):
            self.assertEqual(self.at(hour), [])
        self.assertEqual(self.snapshot(), before)

    def test_long_downtime_does_not_generate_sessions_or_change_the_cursor(self):
        with self.store.db:
            self.store.db.execute("DELETE FROM sessions WHERE guild_id=1")
        before = self.snapshot()
        self.assertEqual(self.at(17, day=date(2030, 10, 17)), [])
        self.assertEqual(self.snapshot(), before)

    def test_non_seminar_weekday_is_ineligible_even_if_a_stale_today_row_exists(self):
        friday = DAY + timedelta(days=1)
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO sessions(guild_id,seminar_date,presenter_id) VALUES(?,?,?)",
                (1, str(friday), 104),
            )
        for hour in (12, 16, 17):
            self.assertEqual(self.at(hour, day=friday), [])

    def test_completed_submission_keeps_noon_announcement_but_suppresses_both_requests(self):
        self.complete()
        self.assertEqual(self.at(12)[0][1], "morning")
        self.assertEqual(self.at(15, 45), [])
        self.assertEqual(self.at(17), [])

    def test_completion_between_the_two_requests_suppresses_the_later_mention(self):
        session, kind = self.at(15, 45)[0]
        self.store.mark_sent(session["id"], str(DAY), kind, 123)
        self.complete()
        self.assertEqual(self.at(16), [])
        self.assertEqual(self.at(17), [])

    def test_partial_upload_or_three_copies_of_one_paper_is_still_incomplete(self):
        self.add_material(DAY, kind="slides")
        self.assertEqual(self.at(15, 45)[0][1], "before")
        for _ in range(3):
            self.add_material(DAY, identity="same-paper")
        self.assertEqual(self.at(17)[0][1], "evening")
        self.add_material(DAY)
        self.add_material(DAY)
        self.assertEqual(self.at(17), [])

    def test_completion_uses_only_the_configured_upload_scope(self):
        session = self.complete(channel_id=999)
        self.assertTrue(self.store.submission(1, session["id"])["complete"])
        self.assertFalse(
            self.store.submission(1, session["id"], visible_channel_ids=[3])["complete"]
        )
        self.assertEqual(self.at(15, 45)[0][0]["id"], session["id"])
        self.assertEqual(self.at(17)[0][0]["id"], session["id"])

    def test_reopening_an_incomplete_submission_does_not_repeat_a_sent_stage(self):
        session, kind = self.at(15, 45)[0]
        self.store.mark_sent(session["id"], str(DAY), kind, 123)
        self.complete()
        paper = self.store.submission(1, session["id"])["documents"][-1]
        self.store.delete_document(1, paper["id"])
        self.assertEqual(self.at(16), [])
        self.assertEqual(self.at(17)[0][1], "evening")

    def test_other_guild_and_past_deliveries_do_not_suppress_todays_valid_stage(self):
        self.store.configure(2, 4, 5, 3, "12:00", "17:00", "Asia/Seoul", NOW)
        with self.store.db:
            other = self.store.db.execute(
                "INSERT INTO sessions(guild_id,seminar_date,presenter_id) VALUES(?,?,?)",
                (2, str(DAY), 201),
            ).lastrowid
        self.store.mark_sent(other, str(DAY), "evening", 123)
        old = self.store.session_on(1, DAY - timedelta(weeks=1))
        self.store.mark_sent(old["id"], str(DAY), "evening", 124)
        self.assertEqual(self.at(17)[0][0]["seminar_date"], str(DAY))

    def test_holiday_suppresses_all_stages_even_with_a_stale_session_row(self):
        with self.store.db:
            self.store.db.execute(
                "INSERT INTO holidays(guild_id,seminar_date) VALUES(?,?)", (1, str(DAY))
            )
        for hour in (12, 16, 17):
            self.assertEqual(self.at(hour), [])
        self.assertEqual(
            self.at(17, day=DAY + timedelta(weeks=1))[0][0]["seminar_date"], "2026-11-05"
        )

    def test_postponement_cancels_today_and_notifies_the_moved_presenter_next_week(self):
        session, kind = self.at(12)[0]
        self.store.mark_sent(session["id"], str(DAY), kind, 123)
        self.store.postpone(1, DAY)
        self.assertEqual(self.at(16), [])
        self.assertEqual(self.at(17), [])
        next_session, next_kind = self.at(12, day=DAY + timedelta(weeks=1))[0]
        self.assertEqual(next_session["id"], session["id"])
        self.assertEqual(next_session["presenter_id"], session["presenter_id"])
        self.assertEqual(next_kind, "morning")

    def test_swap_preserves_sent_stage_and_later_stages_target_the_new_presenter(self):
        session, kind = self.at(12)[0]
        self.store.mark_sent(session["id"], str(DAY), kind, 123)
        new_speaker = self.store.session_on(1, DAY + timedelta(weeks=1))["presenter_id"]
        self.store.swap(1, DAY, DAY + timedelta(weeks=1), DAY)
        self.assertEqual(self.at(13), [])
        before_session, before_kind = self.at(15, 45)[0]
        self.assertEqual((before_session["presenter_id"], before_kind), (new_speaker, "before"))
        self.store.mark_sent(session["id"], str(DAY), "before", 124)
        self.store.swap(1, DAY, DAY + timedelta(weeks=1), DAY)
        self.assertEqual(self.at(16), [])
        self.assertEqual(self.at(17)[0][0]["presenter_id"], session["presenter_id"])

    def test_legacy_configuration_without_before_time_keeps_two_stages(self):
        for config in (
            {**self.config, "before_reminder": None},
            {key: value for key, value in self.config.items() if key != "before_reminder"},
        ):
            self.assertEqual(self.at(16, config=config)[0][1], "morning")
            self.assertEqual(self.at(17, config=config)[0][1], "evening")

    def test_dst_fall_back_does_not_duplicate_a_noon_kind_in_its_repeated_local_hour(self):
        day = date(2026, 11, 1)
        self.store.configure(2, 4, 5, 6, "01:15", "20:00", "America/New_York", NOW)
        self.store.set_rotation(2, [201], day, day)
        self.store.configure(
            2,
            4,
            5,
            6,
            "01:15",
            "20:00",
            "America/New_York",
            NOW,
            notifications_enabled=True,
            reminder_start_date=day,
        )
        config = self.store.config(2)
        due = due_reminders(self.store, config, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
        session, kind = due[0]
        self.assertEqual(kind, "morning")
        self.store.mark_sent(session["id"], str(day), kind, 124)
        self.assertEqual(
            due_reminders(self.store, config, datetime(2026, 11, 1, 6, 30, tzinfo=UTC)), []
        )

    def test_evening_uses_the_local_session_date_across_the_utc_midnight(self):
        day = date(2026, 11, 1)
        self.store.configure(2, 4, 5, 6, "01:15", "20:00", "America/New_York", NOW)
        self.store.set_rotation(2, [201], day, day)
        self.store.configure(
            2,
            4,
            5,
            6,
            "01:15",
            "20:00",
            "America/New_York",
            NOW,
            notifications_enabled=True,
            reminder_start_date=day,
        )
        config = self.store.config(2)
        session, kind = due_reminders(self.store, config, datetime(2026, 11, 2, 1, 30, tzinfo=UTC))[
            0
        ]
        self.assertEqual((session["seminar_date"], kind), ("2026-11-01", "evening"))
        self.store.mark_sent(session["id"], "2026-11-01", kind, 124)
        self.assertEqual(
            due_reminders(self.store, config, datetime(2026, 11, 2, 2, 30, tzinfo=UTC)), []
        )
        self.assertEqual(
            due_reminders(self.store, config, datetime(2026, 11, 2, 5, 0, tzinfo=UTC)), []
        )


if __name__ == "__main__":
    unittest.main()

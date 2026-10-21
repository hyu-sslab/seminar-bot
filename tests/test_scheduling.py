from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from seminar_bot.scheduling import due_reminders
from seminar_bot.store import Store


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.day = date(2026, 10, 15)
        self.store.configure(1, 2, 3, 3, "09:00", "18:00", "Asia/Seoul")
        self.store.set_rotation(1, [101, 102], self.day, self.day)
        self.config = self.store.config(1)

    def tearDown(self):
        self.store.close()

    def at(self, hour, minute=0, day=15):
        return due_reminders(
            self.store, self.config, datetime(2026, 10, day, hour, minute, tzinfo=UTC)
        )

    def test_seoul_morning_boundary(self):
        self.assertEqual(self.at(23, 59, day=14), [])
        self.assertEqual(self.at(0)[0][1], "morning")

    def test_retry_catches_up_morning_but_not_after_evening(self):
        self.assertEqual(self.at(5)[0][1], "morning")
        self.assertEqual(self.at(9)[0][1], "evening")
        self.assertEqual(len(self.at(9)), 1)

    def test_delivery_log_suppresses_restart_replay(self):
        session, kind = self.at(0)[0]
        self.store.mark_sent(session["id"], session["seminar_date"], kind, 123)
        self.assertEqual(self.at(1), [])
        self.assertEqual(self.at(9)[0][1], "evening")

    def test_non_seminar_day_and_previous_days_are_not_replayed(self):
        self.assertEqual(self.at(0, day=16), [])
        self.assertEqual(self.at(15), [])  # midnight the following day in Seoul

    def test_postponement_cancels_evening_and_next_week_reminds_again(self):
        session, kind = self.at(0)[0]
        self.store.mark_sent(session["id"], session["seminar_date"], kind, 123)
        self.store.postpone(1, self.day)
        self.assertEqual(self.at(9), [])
        next_due = due_reminders(self.store, self.config, datetime(2026, 10, 22, 0, tzinfo=UTC))
        self.assertEqual(next_due[0][0]["presenter_id"], 101)
        self.assertEqual(next_due[0][1], "morning")

    def test_swap_after_morning_notifies_new_speaker(self):
        session, kind = self.at(0)[0]
        self.store.mark_sent(session["id"], session["seminar_date"], kind, 123)
        self.store.swap(1, self.day, self.day + timedelta(weeks=1), self.day)
        self.assertEqual(self.at(1)[0][0]["presenter_id"], 102)

    def test_dst_fall_back_local_time_does_not_duplicate(self):
        day = date(2026, 11, 1)
        self.store.configure(2, 4, 5, 6, "01:15", "20:00", "America/New_York")
        self.store.set_rotation(2, [201], day, day)
        config = self.store.config(2)
        due = due_reminders(self.store, config, datetime(2026, 11, 1, 5, 30, tzinfo=UTC))
        session, kind = due[0]
        self.store.mark_sent(session["id"], session["seminar_date"], kind, 124)
        self.assertEqual(
            due_reminders(self.store, config, datetime(2026, 11, 1, 6, 30, tzinfo=UTC)), []
        )


if __name__ == "__main__":
    unittest.main()

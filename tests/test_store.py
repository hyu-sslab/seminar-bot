from __future__ import annotations

import hashlib
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from seminar_bot.documents import DocumentInfo
from seminar_bot.store import Store, UserError, parse_date, parse_time

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

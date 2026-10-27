from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from seminar_bot.documents import DocumentInfo
from seminar_bot.store import Store, UserError

DAY = date(2026, 10, 15)
NOW = datetime(2026, 10, 15, 0, tzinfo=UTC)


class SubmissionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "seminar.sqlite3"
        self.store = Store(self.path)
        self.store.configure(1, 1001, 1002, 3, "09:00", "18:00", "Asia/Seoul", NOW)
        self.store.set_rotation(1, [101, 102], DAY, DAY)
        self.session = self.store.session_on(1, DAY)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def add(self, index, kind="paper", filename=None, sha=None, doi=None, title=None):
        info = DocumentInfo(
            title or f"Distinct Research Paper Number {index}",
            "text",
            doi,
            None,
            sha or f"hash-{index}",
            "pdf_metadata",
        )
        return self.store.add_document(
            1,
            index,
            101,
            info,
            filename or f"paper{index}.pdf",
            f"files/{index}.pdf",
            f"https://discord.com/channels/1/1002/{index}",
            NOW,
            kind=kind,
            classification_reason="test fixture",
        )[0]

    def status(self):
        return self.store.submission(1, self.session["id"])

    def test_split_uploads_accumulate_and_require_slides_plus_three_papers(self):
        self.add(1, "slides", "seminar.pptx")
        self.add(2)
        self.add(3)
        self.assertEqual((self.status()["slides"], self.status()["papers"]), (1, 2))
        self.assertFalse(self.status()["complete"])
        self.add(4)
        self.assertTrue(self.status()["complete"])
        self.add(5)
        self.assertEqual(self.status()["papers"], 4)

    def test_three_papers_alone_are_incomplete_and_duplicates_do_not_fill_slots(self):
        self.add(1, doi="10.1234/same")
        self.add(2, doi="10.1234/same")
        self.add(3)
        status = self.status()
        self.assertEqual((status["slides"], status["papers"], status["missing_papers"]), (0, 2, 1))
        self.assertFalse(status["complete"])

    def test_unknown_files_only_count_after_authorized_classification(self):
        record_id = self.add(1, "unknown", "unknown.pdf")
        self.assertEqual(self.status()["unknown"], 1)
        with self.assertRaises(UserError):
            self.store.classify(1, record_id, "paper", 999, False)
        self.store.classify(1, record_id, "paper", 101, False)
        self.assertEqual((self.status()["papers"], self.status()["unknown"]), (1, 0))
        self.store.classify(1, record_id, "slides", 999, True)
        self.assertEqual((self.status()["papers"], self.status()["slides"]), (0, 1))

    def test_powerpoint_cannot_be_classified_as_paper(self):
        for index, extension in enumerate(("ppt", "pptx"), start=1):
            record_id = self.add(index, "slides", f"slides.{extension}")
            with self.assertRaises(UserError):
                self.store.classify(1, record_id, "paper", 101, False)
        with self.assertRaises(UserError):
            self.add(3, "paper", "not-a-paper.ppt")

    def test_explicit_paper_classification_clears_body_and_archive_reference(self):
        record_id = self.add(1, "slides", "slides.pdf", doi="10.1000/paper")
        before = self.store.document(1, record_id)
        self.assertTrue(before["file_path"])
        self.assertEqual(before["extracted_text"], "text")
        paper = self.store.classify(1, record_id, "paper", 101, False)
        self.assertEqual(
            (paper["kind"], paper["file_path"], paper["extracted_text"]), ("paper", "", "")
        )
        self.assertEqual(
            (paper["title"], paper["doi"], paper["sha256"]),
            (before["title"], before["doi"], before["sha256"]),
        )
        restored = self.store.classify(1, record_id, "slides", 101, False)
        self.assertEqual((restored["file_path"], restored["extracted_text"]), ("", ""))

    def test_duplicate_slide_bytes_cannot_fill_a_paper_slot(self):
        self.add(1, "slides", "slides.pdf", sha="same")
        self.add(2, "paper", "paper.pdf", sha="same")
        self.assertEqual((self.status()["slides"], self.status()["papers"]), (1, 0))

    def test_bundle_follows_presenter_on_swap_and_whole_week_postponement(self):
        for index, kind in enumerate(("slides", "paper", "paper", "paper"), start=1):
            self.add(index, kind)
        self.store.swap(1, DAY, DAY + timedelta(weeks=1), DAY)
        moved = self.store.session_on(1, DAY + timedelta(weeks=1))
        self.assertEqual(moved["presenter_id"], 101)
        self.assertTrue(self.store.submission(1, moved["id"])["complete"])
        self.assertEqual(len(self.status()["documents"]), 0)
        self.store.postpone(1, DAY)
        self.assertTrue(self.store.submission(1, moved["id"])["complete"])
        self.assertEqual(self.store.session(1, moved["id"])["seminar_date"], "2026-10-29")

    def test_status_and_search_respect_source_channel_permissions(self):
        self.add(1, "slides", "slides.pdf")
        self.add(2)
        self.assertEqual(self.store.submission(1, self.session["id"], [999])["papers"], 0)
        self.assertEqual(self.store.submission(1, self.session["id"], [1002])["papers"], 1)
        self.assertEqual(self.store.search(1, "distinct", kind="paper")[1], 1)
        self.assertEqual(self.store.search(1, "distinct", kind="slides")[1], 1)
        self.assertEqual(
            self.store.search(1, "distinct", kind="paper", visible_channel_ids=[999])[1], 0
        )

    def test_progress_survives_restart(self):
        self.add(1, "slides", "presentation.ppt")
        self.add(2)
        before = self.status()
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.status(), before)

    def test_v1_migration_preserves_ids_history_and_files(self):
        paper_id = self.add(1, "unknown", "paper.pdf")
        slides_id = self.add(2, "unknown", "presentation.pptx")
        self.store.mark_sent(self.session["id"], DAY.isoformat(), "morning", 990)
        old_file = self.store.document(1, paper_id)["file_path"]
        self.store.close()
        # Recreate the actual v1 layout rather than wiping a user's database.
        with sqlite3.connect(self.path) as legacy:
            legacy.execute("ALTER TABLE documents DROP COLUMN classification_reason")
            legacy.execute("ALTER TABLE documents DROP COLUMN kind")
            legacy.execute("PRAGMA user_version=1")
        self.store = Store(self.path)
        self.assertEqual(self.store.document(1, paper_id)["kind"], "paper")
        self.assertEqual(self.store.document(1, slides_id)["kind"], "slides")
        self.assertEqual(self.store.document(1, paper_id)["file_path"], old_file)
        self.assertEqual(self.store.document(1, paper_id)["session_id"], self.session["id"])
        self.assertTrue(self.store.was_sent(self.session["id"], DAY.isoformat(), "morning"))
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 10)
        # Opening the migrated database again must preserve manual corrections.
        self.store.classify(1, paper_id, "slides", 101, False)
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.store.document(1, paper_id)["kind"], "slides")


if __name__ == "__main__":
    unittest.main()

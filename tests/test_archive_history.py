from __future__ import annotations

import copy
import hashlib
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from seminar_bot.documents import DocumentInfo
from seminar_bot.scheduling import due_reminders
from seminar_bot.store import Store, UserError
from seminar_bot.submission_access import SubmissionAccess
from seminar_bot.submissions import count_submission

NOW = datetime(2026, 10, 27, 20, tzinfo=UTC)
DAY = date(2026, 10, 15)


def material(key="paper", *, kind="paper", sha=True, title="Previously Presented Research"):
    return {
        "import_key": "import:" + key,
        "info": DocumentInfo(
            title,
            "private source text\nnever retained for papers",
            "10.1234/" + key,
            None,
            hashlib.sha256(key.encode()).hexdigest() if sha else "",
            "pdf_text",
        ),
        "filename": key + ".pdf",
        "kind": kind,
        "file_path": "files/imports/" + key + ".pdf" if kind == "slides" else "",
        "venue": "Example Conference",
        "year": 2025,
    }


def group(key="group", *, documents=None, year=2026, day=None, presenter=101, name="과거 발표자"):
    return {
        "import_key": "group:" + key,
        "presenter_id": presenter,
        "presenter_name_snapshot": name,
        "seminar_year": year,
        "seminar_date": day,
        "label": "과거 자료",
        "archive_kind": "presentation",
        "documents": documents if documents is not None else [material(key)],
    }


class ArchiveHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "archive.sqlite3"
        self.store = Store(self.path)
        self.store.configure(1, 11, 12, 3, "09:00", "18:00", "UTC", NOW)
        with self.store.db:
            self.session_id = self.store.db.execute(
                "INSERT INTO sessions(guild_id,seminar_date,presenter_id) VALUES(1,?,101)",
                (DAY.isoformat(),),
            ).lastrowid
        self.store.set_rotation(1, [101, 102], date(2026, 10, 29), NOW.date())
        self.store.configure(
            1,
            11,
            12,
            3,
            "09:00",
            "18:00",
            "UTC",
            NOW,
            notifications_enabled=True,
            reminder_start_date=DAY,
        )

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def apply(self, groups, **kwargs):
        return self.store.insert_archive_batch(1, groups, imported_at=NOW, **kwargs)

    def snapshot(self):
        tables = [
            "guilds",
            "sessions",
            "holidays",
            "deliveries",
            "rotation_file_state",
            "web_submission_receipts",
            "submission_invitations",
            "submission_grants",
            "web_upload_tombstones",
        ]
        return {
            table: [
                tuple(row) for row in self.store.db.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in tables
        }

    def test_year_only_history_does_not_change_schedules_reminders_or_default_submission(self):
        access = SubmissionAccess(self.store)
        selected = access.select_session(1, 101, NOW.date())
        before = self.snapshot()
        reminders = due_reminders(self.store, self.store.require_config(1), NOW)
        result = self.apply([group()])
        self.assertEqual(result["inserted_documents"], 1)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(due_reminders(self.store, self.store.require_config(1), NOW), reminders)
        self.assertEqual(access.select_session(1, 101, NOW.date()), selected)
        self.assertEqual(self.snapshot(), before)
        record = self.store.document(1, result["documents"][0]["id"])
        self.assertIsNone(record["session_id"])
        self.assertIsNone(record["seminar_date"])
        self.assertEqual(record["seminar_year"], 2026)
        self.assertEqual(record["date_precision"], "year")
        self.assertEqual(record["presenter_name_snapshot"], "과거 발표자")
        self.assertEqual(record["source_type"], "import")
        self.assertEqual(record["extracted_text"], "")
        self.assertEqual(record["file_path"], "")
        self.assertEqual(self.store.submission(1, self.session_id)["documents"], [])

    def test_operational_and_archive_ids_can_overlap_without_merging_their_documents(self):
        info = material("operational")["info"]
        normal, _ = self.store.add_document(
            1,
            55,
            101,
            info,
            "current.pdf",
            "",
            "",
            NOW,
            kind="paper",
            session_id=self.session_id,
            source_type="web",
            upload_key="web-current",
        )
        imported = self.apply(
            [
                group("unknown"),
                group("dated", day=date(2026, 9, 1)),
                group("older", year=2025, day=date(2025, 12, 1)),
            ]
        )
        rows, total = self.store.presentations(1, NOW.date(), visible_channel_ids=[12])
        self.assertEqual(total, 4)
        self.assertEqual(
            [row["session"]["record_key"] for row in rows],
            [
                "session:1",
                "archive:2",
                "archive:1",
                "archive:3",
            ],
        )
        self.assertEqual(
            [row["session"]["seminar_date"] for row in rows],
            [
                "2026-10-15",
                "2026-09-01",
                None,
                "2025-12-01",
            ],
        )
        self.assertEqual(
            [row["session"]["date_precision"] for row in rows], ["day", "day", "year", "day"]
        )
        self.assertEqual(rows[0]["documents"][0]["id"], normal)
        self.assertEqual(rows[2]["documents"][0]["id"], imported["documents"][0]["id"])
        self.assertEqual(self.store.presentations(1, NOW.date(), page_size=2, page=2)[0], rows[2:])

    def test_search_aliases_and_snapshot_name_search_keep_unknown_dates_unknown(self):
        result = self.apply([group(name="과거 이름", documents=[material("known")])])
        record_id = result["documents"][0]["id"]
        for records, count in [
            self.store.search(1, "10.1234/known"),
            self.store.archive(1, q="research"),
        ]:
            self.assertEqual(count, 1)
            self.assertEqual(records[0]["id"], record_id)
            self.assertIsNone(records[0]["seminar_date"])
            self.assertEqual(records[0]["seminar_year"], 2026)
            self.assertTrue(records[0]["archived"])
        for query in ["과거이름", "과거 이름", "2026"]:
            rows, total = self.store.presentations(1, NOW.date(), q=query)
            self.assertEqual(total, 1, query)
            self.assertEqual(rows[0]["documents"][0]["id"], record_id)

    def test_reimport_is_idempotent_and_never_overwrites_a_later_title_edit(self):
        first = self.apply([group()])
        record_id = first["documents"][0]["id"]
        self.store.rename_document(1, record_id, "Reviewed corrected title", 101, False)
        again = self.apply([group()])
        self.assertEqual(again["inserted_groups"], 0)
        self.assertEqual(again["inserted_documents"], 0)
        self.assertEqual(again["skipped_documents"], 1)
        self.assertEqual(again["documents"][0]["id"], record_id)
        self.assertEqual(self.store.document(1, record_id)["title"], "Reviewed corrected title")

    def test_content_conflict_rolls_back_the_entire_batch_and_preserves_existing_history(self):
        first = self.apply([group()])
        changed = group()
        changed["documents"][0]["info"] = DocumentInfo(
            "Other", "", None, None, "b" * 64, "pdf_text"
        )
        with self.assertRaises(UserError):
            self.apply([group("new"), changed])
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM archive_presentations").fetchone()[0], 1
        )
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 1)
        self.assertEqual(
            self.store.document(1, first["documents"][0]["id"])["title"],
            "Previously Presented Research",
        )

    def test_changed_package_metadata_conflicts_even_when_the_original_bytes_are_identical(self):
        first = self.apply([group()])
        for field, value in [("title", "Changed package title"), ("doi", "10.1234/changed")]:
            changed = group()
            changed["documents"][0]["info"] = replace(
                changed["documents"][0]["info"], **{field: value}
            )
            with self.subTest(field=field), self.assertRaises(UserError):
                self.apply([changed])
        self.assertEqual(
            self.store.document(1, first["documents"][0]["id"])["title"],
            "Previously Presented Research",
        )

    def test_caller_can_check_invariants_before_commit_or_rollback(self):
        self.apply([group()], commit=False)
        self.assertTrue(self.store.db.in_transaction)
        with sqlite3.connect(self.path) as other:
            self.assertEqual(
                other.execute("SELECT COUNT(*) FROM archive_presentations").fetchone()[0], 0
            )
        self.store.db.rollback()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)
        self.store.db.execute("BEGIN IMMEDIATE")
        self.apply([group()])
        self.assertTrue(self.store.db.in_transaction)
        self.store.db.rollback()
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0)

    def test_deleted_imports_are_not_resurrected_and_the_ledger_survives(self):
        first = self.apply([group()])
        self.store.delete_document(1, first["documents"][0]["id"])
        result = self.apply([group()])
        self.assertEqual(result["tombstoned_documents"], 1)
        self.assertEqual(result["inserted_documents"], 0)
        self.assertIsNone(result["documents"][0]["id"])
        self.assertIsNone(
            self.store.db.execute("SELECT document_id FROM archive_import_items").fetchone()[0]
        )
        self.assertEqual(self.store.presentations(1, NOW.date()), ([], 0))

    def test_same_paper_across_groups_is_preserved_and_metadata_only_duplicates_count_once(self):
        one = material("same-one", sha=False)
        two = material("same-two", sha=False)
        one["info"] = DocumentInfo("Same Historical Paper", "not stored", None, None, "", "manual")
        two["info"] = one["info"]
        result = self.apply(
            [
                group("one", documents=[one, two]),
                group("two", documents=[{**one, "import_key": "import:other-group"}]),
            ]
        )
        self.assertEqual(result["inserted_documents"], 3)
        rows, total = self.store.presentations(1, NOW.date())
        self.assertEqual(total, 2)
        for row in rows:
            self.assertEqual(count_submission(row["documents"])["papers"], 1)
            for document in row["documents"]:
                self.assertEqual(document["sha256"], "")
                self.assertEqual(document["extraction_method"], "user_metadata")
        conflicting = copy.deepcopy(one)
        conflicting["info"] = DocumentInfo("Different Paper", "", None, None, "", "manual")
        with self.assertRaises(UserError):
            self.apply([group("one", documents=[conflicting])])

    def test_slide_paths_stay_inside_files_and_original_paper_content_is_never_stored(self):
        paper = material()
        paper["file_path"] = "/private/source.pdf"
        self.apply([group(documents=[paper, material("slides", kind="slides")])])
        records = self.store.archive(1)[0]
        for record in records:
            if record["kind"] == "paper":
                self.assertEqual((record["file_path"], record["extracted_text"]), ("", ""))
            else:
                self.assertEqual(record["file_path"], "files/imports/slides.pdf")
                self.assertIn("\n", record["extracted_text"])
        for path in [
            "../outside.pdf",
            "/files/outside.pdf",
            "files/../outside.pdf",
            "data/slides.pdf",
            "files/a.pptx",
        ]:
            item = material("new-slide", kind="slides")
            item["file_path"] = path
            with self.subTest(path=path), self.assertRaises(UserError):
                self.apply([group("bad", documents=[item])])

    def test_channel_guild_visibility_and_foreign_key_invariants_are_preserved(self):
        first = self.apply([group()])
        record_id = first["documents"][0]["id"]
        self.store.configure(2, 21, 12, 3, "09:00", "18:00", "UTC", NOW)
        self.store.insert_archive_batch(2, [group("other")], imported_at=NOW)
        self.assertEqual(self.store.archive(1)[1], 1)
        with self.store.db:
            self.store.db.execute(
                "UPDATE documents SET source_channel_id=99 WHERE id=?", (record_id,)
            )
        self.assertEqual(self.store.archive(1, visible_channel_ids=[12]), ([], 0))
        self.assertEqual(self.store.search(1, "research", visible_channel_ids=[12]), ([], 0))
        self.assertEqual(self.store.presentations(1, NOW.date(), visible_channel_ids=[12]), ([], 0))
        with self.assertRaises(sqlite3.IntegrityError), self.store.db:
            self.store.db.execute(
                "UPDATE documents SET session_id=? WHERE id=?", (self.session_id, record_id)
            )
        with self.assertRaises(sqlite3.IntegrityError), self.store.db:
            self.store.db.execute("UPDATE documents SET guild_id=2 WHERE id=?", (record_id,))
        self.assertEqual(self.store.db.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_invalid_future_or_fabricated_dates_and_general_import_source_are_rejected(self):
        for changes in [
            {"seminar_year": 2027},
            {"seminar_date": date(2025, 1, 1)},
            {"seminar_date": "2026-01-01"},
            {"seminar_date": date(2026, 12, 1)},
            {"presenter_id": True},
            {"archive_kind": "future"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(UserError):
                self.apply([{**group(), **changes}])
        with self.assertRaises(UserError):
            self.store.add_document(
                1, 5, 101, material()["info"], "paper.pdf", "", "", NOW, source_type="import"
            )
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM archive_presentations").fetchone()[0], 0
        )

    def test_schema_eight_upgrade_preserves_operational_rows_and_existing_documents(self):
        record_id, _ = self.store.add_document(
            1,
            77,
            101,
            material()["info"],
            "current.pdf",
            "",
            "",
            NOW,
            kind="paper",
            session_id=self.session_id,
            source_type="web",
            upload_key="existing-web",
        )
        before = self.snapshot()
        original = dict(
            self.store.db.execute("SELECT * FROM documents WHERE id=?", (record_id,)).fetchone()
        )
        original.pop("archive_presentation_id")
        self.store.close()
        with sqlite3.connect(self.path) as old:
            old.execute("DROP TABLE archive_import_items")
            old.execute("DROP TRIGGER archive_document_insert")
            old.execute("DROP TRIGGER archive_document_update")
            old.execute("DROP INDEX document_archive")
            old.execute("ALTER TABLE documents DROP COLUMN archive_presentation_id")
            old.execute("DROP TABLE archive_presentations")
            old.execute("PRAGMA user_version=8")
        self.store = Store(self.path)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 10)
        self.assertEqual(self.snapshot(), before)
        upgraded = dict(
            self.store.db.execute("SELECT * FROM documents WHERE id=?", (record_id,)).fetchone()
        )
        self.assertIsNone(upgraded.pop("archive_presentation_id"))
        self.assertEqual(upgraded, original)
        self.assertEqual(self.store.db.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()

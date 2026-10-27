from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import tempfile
import unittest
import zipfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

from seminar_bot import archive_import
from seminar_bot.__main__ import instance_lock
from seminar_bot.documents import DocumentInfo
from seminar_bot.store import Store

NOW = datetime(2024, 5, 6, 1, tzinfo=UTC)
SLIDES = b"%PDF-1.7\nSynthetic historical presentation\n%%EOF"


def fixture_manifest():
    sha = hashlib.sha256(SLIDES).hexdigest()
    paper_sha = hashlib.sha256(b"original paper was reviewed separately").hexdigest()

    def document(key, title, kind="paper", original_sha=paper_sha):
        return {
            "import_key": key,
            "filename": "talk.pdf" if kind == "slides" else "reading.pdf",
            "kind": kind,
            "info": asdict(DocumentInfo(title, "", None, None, original_sha, "filename")),
        }

    slide = document("fixture:group-one:slides", "Historical Talk", "slides", sha)
    slide["asset"] = "slides/talk.pdf"
    return {
        "schema_version": 1,
        "batch_id": "fixture-collection",
        "groups": [
            {
                "import_key": "fixture:group-one",
                "presenter_name_snapshot": "Presenter One",
                "presenter_id": None,
                "seminar_year": 2024,
                "seminar_date": "2024-05-06",
                "archive_kind": "presentation",
                "label": "Reviewed historical presentation",
                "documents": [
                    slide,
                    document("fixture:group-one:paper-one", "Example Systems Study"),
                    document("fixture:group-one:metadata", "Metadata Only Study", original_sha=""),
                ],
            },
            {
                "import_key": "fixture:unassigned",
                "presenter_name_snapshot": "Presenter Two",
                "seminar_year": 2024,
                "seminar_date": None,
                "archive_kind": "unassigned",
                "label": "Presented papers with unknown session",
                "documents": [document("fixture:unassigned:paper-one", "Example Systems Study")],
            },
        ],
    }


class ArchiveImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "seminar.sqlite3"
        store = Store(self.database)
        store.configure(1, 1001, 1002, 0, "09:00", "18:00", "Asia/Seoul", NOW)
        store.set_rotation(1, [101, 102], NOW.date(), NOW.date())
        session = store.session_on(1, NOW.date())
        store.mark_sent(session["id"], str(NOW.date()), "morning", 901)
        store.close()
        self.package = self.root / "reviewed.zip"
        self.write_package(fixture_manifest())

    def tearDown(self):
        self.temporary.cleanup()

    def write_package(self, manifest, extra=None):
        with zipfile.ZipFile(self.package, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("slides/talk.pdf", SLIDES)
            for name, value in (extra or {}).items():
                archive.writestr(name, value)

    def connect(self):
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        return connection

    def test_dry_run_uses_store_only_on_a_disposable_copy_and_never_changes_source(self):
        before = self.database.read_bytes()
        original = Store.__init__
        constructed = []

        def construct(store, path):
            constructed.append(Path(path))
            original(store, path)

        with patch.object(Store, "__init__", construct):
            result = archive_import.run_import(self.package, self.database)
        self.assertFalse(result["applied"])
        self.assertTrue(result["validated_on_isolated_copy"])
        self.assertEqual(result["would_insert_groups"], 2)
        self.assertEqual(result["would_insert_documents"], 4)
        self.assertTrue(constructed)
        self.assertNotIn(self.database, constructed)
        self.assertEqual(self.database.read_bytes(), before)
        self.assertFalse((self.root / "files").exists())
        self.assertFalse((self.root / "backups").exists())
        self.assertFalse((self.root / "bot.lock").exists())

    def test_apply_preserves_runtime_and_repeated_paper_history_and_is_idempotent(self):
        with self.connect() as connection:
            before = archive_import.runtime_snapshot(connection)
        result = archive_import.run_import(self.package, self.database, apply=True)
        self.assertTrue(result["runtime_preserved"])
        self.assertEqual(result["inserted_documents"], 4)
        self.assertEqual(result["inserted_groups"], 2)
        with self.connect() as connection:
            archive_import.verify_runtime_snapshot(connection, before)
            rows = connection.execute("SELECT * FROM documents ORDER BY id").fetchall()
            papers = [row for row in rows if row["kind"] == "paper"]
            self.assertEqual(len(papers), 3)
            self.assertTrue(all(row["session_id"] is None for row in rows))
            self.assertTrue(all(row["file_path"] == row["extracted_text"] == "" for row in papers))
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM documents WHERE title='Example Systems Study'"
                ).fetchone()[0],
                2,
            )
            slide = next(row for row in rows if row["kind"] == "slides")
            self.assertEqual((self.root / slide["file_path"]).read_bytes(), SLIDES)
            connection.execute(
                "UPDATE documents SET title='Locally Edited Title' WHERE id=?", (slide["id"],)
            )
        again = archive_import.run_import(self.package, self.database, apply=True)
        self.assertEqual(again["inserted_documents"], 0)
        self.assertEqual(again["skipped_documents"], 4)
        with self.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT title FROM documents WHERE id=?", (slide["id"],)
                ).fetchone()[0],
                "Locally Edited Title",
            )

    def test_sqlite_backup_includes_committed_wal_and_precedes_import(self):
        writer = sqlite3.connect(self.database)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE import_test_probe(value TEXT)")
        writer.execute("INSERT INTO import_test_probe VALUES('committed in WAL')")
        writer.commit()
        try:
            self.assertTrue(Path(str(self.database) + "-wal").exists())
            result = archive_import.run_import(self.package, self.database, apply=True)
            with sqlite3.connect(result["backup"]) as backup:
                self.assertEqual(
                    backup.execute("SELECT value FROM import_test_probe").fetchone()[0],
                    "committed in WAL",
                )
                self.assertEqual(backup.execute("SELECT count(*) FROM documents").fetchone()[0], 0)
        finally:
            writer.close()

    def test_apply_refuses_a_running_bot_lock(self):
        with instance_lock(self.root), self.assertRaises(ValueError):
            archive_import.run_import(self.package, self.database, apply=True)
        self.assertFalse((self.root / "backups").exists())

    def test_deleted_upload_is_not_resurrected_or_copied(self):
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO web_upload_tombstones VALUES(?,?)", (1, "fixture:group-one:slides")
            )
        result = archive_import.run_import(self.package, self.database, apply=True)
        self.assertEqual(result["inserted_documents"], 3)
        self.assertEqual(result["would_skip_deleted_documents"], 1)
        self.assertFalse((self.root / "files").exists())

    def test_metadata_revision_with_same_fixed_key_conflicts_during_dry_run(self):
        archive_import.run_import(self.package, self.database, apply=True)
        revised = fixture_manifest()
        revised["groups"][0]["documents"][1]["info"]["title"] = "Changed Manifest Metadata"
        self.write_package(revised)
        with self.assertRaises(ValueError):
            archive_import.run_import(self.package, self.database)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM documents").fetchone()[0], 4)

    def test_reimport_detects_missing_or_modified_existing_slide_files(self):
        archive_import.run_import(self.package, self.database, apply=True)
        with self.connect() as connection:
            relative = connection.execute(
                "SELECT file_path FROM documents WHERE kind='slides'"
            ).fetchone()[0]
        path = self.root / relative
        path.write_bytes(b"modified existing asset")
        with self.assertRaises(ValueError):
            archive_import.run_import(self.package, self.database)
        path.unlink()
        with self.assertRaises(ValueError):
            archive_import.run_import(self.package, self.database, apply=True)
        self.assertFalse(path.exists())

    def test_directory_manifest_changes_cannot_rebind_already_verified_assets(self):
        manifest = fixture_manifest()
        other_bytes = b"%PDF-1.7\nA different synthetic talk\n%%EOF"
        other = json.loads(json.dumps(manifest["groups"][0]["documents"][0]))
        other.update(
            import_key="fixture:group-one:other-slides",
            filename="other.pdf",
            asset="slides/other.pdf",
        )
        other["info"]["sha256"] = hashlib.sha256(other_bytes).hexdigest()
        manifest["groups"][0]["documents"].append(other)
        directory = self.root / "prepared"
        (directory / "slides").mkdir(parents=True)
        manifest_path = directory / "manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        (directory / "slides/talk.pdf").write_bytes(SLIDES)
        (directory / "slides/other.pdf").write_bytes(other_bytes)
        original = archive_import.ImportPackage.copy_verified
        staged = 0

        def copy(package, asset, sha, destination=None):
            nonlocal staged
            result = original(package, asset, sha, destination)
            if destination is not None:
                staged += 1
                if staged == 2:
                    changed = json.loads(json.dumps(manifest))
                    changed["groups"][0]["documents"][0]["asset"] = "slides/other.pdf"
                    manifest_path.write_text(json.dumps(changed))
            return result

        with patch.object(archive_import.ImportPackage, "copy_verified", copy):
            archive_import.run_import(directory, self.database, apply=True)
        with self.connect() as connection:
            for row in connection.execute(
                "SELECT file_path,sha256 FROM documents WHERE kind='slides'"
            ):
                self.assertEqual(
                    hashlib.sha256((self.root / row[0]).read_bytes()).hexdigest(), row[1]
                )

    def test_failed_invariant_rolls_back_rows_and_new_asset_links(self):
        original = archive_import.verify_runtime_snapshot

        def verify(connection, snapshot):
            path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
            if (
                path == self.database
                and connection.execute("SELECT count(*) FROM documents").fetchone()[0]
            ):
                connection.execute("UPDATE guilds SET next_index=99")
            original(connection, snapshot)

        with (
            patch.object(archive_import, "verify_runtime_snapshot", verify),
            self.assertRaises(ValueError),
        ):
            archive_import.run_import(self.package, self.database, apply=True)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM documents").fetchone()[0], 0)
            self.assertNotEqual(
                connection.execute("SELECT next_index FROM guilds").fetchone()[0], 99
            )
        self.assertEqual(list((self.root / "files").rglob("*.pdf")), [])

    def test_postcommit_verification_error_keeps_committed_assets(self):
        original = archive_import.verify_runtime_snapshot

        def verify(connection, snapshot):
            path = Path(connection.execute("PRAGMA database_list").fetchone()[2])
            if (
                path == self.database
                and not connection.in_transaction
                and connection.execute("SELECT count(*) FROM documents").fetchone()[0]
            ):
                raise ValueError("postcommit inspection unavailable")
            original(connection, snapshot)

        with (
            patch.object(archive_import, "verify_runtime_snapshot", verify),
            self.assertRaises(ValueError),
        ):
            archive_import.run_import(self.package, self.database, apply=True)
        with self.connect() as connection:
            slide = connection.execute(
                "SELECT file_path FROM documents WHERE kind='slides'"
            ).fetchone()
            self.assertIsNotNone(slide)
            self.assertEqual((self.root / slide[0]).read_bytes(), SLIDES)

    def test_package_rejects_paper_originals_bad_hashes_paths_and_links(self):
        invalid = fixture_manifest()
        invalid["groups"][0]["documents"][1]["asset"] = "papers/original.pdf"
        self.write_package(invalid, {"papers/original.pdf": b"private original"})
        with self.assertRaises(ValueError):
            archive_import.run_import(self.package, self.database)
        invalid = fixture_manifest()
        invalid["groups"][0]["documents"][0]["info"]["sha256"] = "a" * 64
        self.write_package(invalid)
        with self.assertRaises(ValueError):
            archive_import.run_import(self.package, self.database)
        self.write_package(fixture_manifest(), {"../outside.pdf": b"unexpected"})
        with self.assertRaises(ValueError):
            archive_import.run_import(self.package, self.database)
        self.write_package(fixture_manifest())
        link = zipfile.ZipInfo("linked.pdf")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(self.package, "a") as archive:
            archive.writestr(link, "/private/source.pdf")
        with self.assertRaises(ValueError):
            archive_import.run_import(self.package, self.database)

    def test_declared_asset_limit_is_checked_before_decompression(self):
        with patch.object(archive_import, "MAX_ASSET_BYTES", 8), self.assertRaises(ValueError):
            archive_import.run_import(self.package, self.database)

    def test_explicit_private_presenter_mapping_and_guild_selection(self):
        store = Store(self.database)
        store.configure(2, 2001, 2002, 0, "09:00", "18:00", "Asia/Seoul", NOW)
        store.close()
        with self.assertRaises(ValueError):
            archive_import.run_import(self.package, self.database)
        mapping = self.root / "private-presenters.json"
        mapping.write_text(json.dumps({"Presenter One": 101, "Presenter Two": 102}))
        result = archive_import.run_import(
            self.package, self.database, guild_id=1, presenter_map=mapping
        )
        self.assertEqual(result["unresolved_presenter_groups"], 0)

    def test_runtime_snapshot_detects_duplicate_rows_in_tables_without_primary_key(self):
        with self.connect() as connection:
            connection.execute("CREATE TABLE duplicate_probe(value TEXT)")
            connection.execute("INSERT INTO duplicate_probe VALUES('same')")
            before = archive_import.runtime_snapshot(connection)
            connection.execute("INSERT INTO duplicate_probe VALUES('same')")
            with self.assertRaises(ValueError):
                archive_import.verify_runtime_snapshot(connection, before)


if __name__ == "__main__":
    unittest.main()

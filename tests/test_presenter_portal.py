from __future__ import annotations

import asyncio
import hashlib
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_submission_access as fixtures

from seminar_bot.documents import DocumentInfo
from seminar_bot.store import Store
from seminar_bot.submission_access import SubmissionAccess, SubmissionAccessDenied, session_version
from seminar_bot.web_api import document_version


class PresenterPortalTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.SubmissionAccessAPITests.asyncSetUp
    asyncTearDown = fixtures.SubmissionAccessAPITests.asyncTearDown
    headers = fixtures.SubmissionAccessAPITests.headers

    async def grant(self, *, session=None, scope="presenter"):
        row = session or self.row
        invitation, _ = self.access.issue(1, row["id"], 101, scope=scope)
        token, _ = self.access.redeem(invitation, 1)
        return token

    async def paper(self, grant, fields=None, *, session=None, key=None):
        row = session or self.row
        return await self.client.post(
            f"/v1/sessions/{row['id']}/papers",
            json=fields or {"title": "A useful scientific study"},
            headers=self.headers(
                grant,
                **{
                    "X-Seminar-Version": session_version(row),
                    "Idempotency-Key": key or str(uuid.uuid4()),
                },
            ),
        )

    async def mutate(
        self, grant, record, fields=None, *, method="patch", session=None, version=None
    ):
        row = session or self.row
        return await getattr(self.client, method)(
            f"/v1/sessions/{row['id']}/documents/{record['id']}",
            json={} if method == "delete" else fields or {"title": "Revised scientific study"},
            headers=self.headers(
                grant,
                **{
                    "X-Seminar-Version": session_version(row),
                    "X-Document-Version": version or record["revision"],
                },
            ),
        )

    async def uploaded(self, grant, *, kind="slides", title="Saved Presentation", sha=None):
        key = str(uuid.uuid4())
        body = b"%PDF-test" + key.encode()
        self.bot.extract_document = AsyncMock(
            return_value=DocumentInfo(
                title,
                "temporary full body",
                None,
                None,
                sha or hashlib.sha256(body).hexdigest(),
                "pdf_metadata",
            )
        )
        response = await self.client.put(
            f"/v1/sessions/{self.row['id']}/documents?kind={kind}",
            data=body,
            headers=self.headers(
                grant,
                **{
                    "Content-Type": "application/octet-stream",
                    "X-Filename": "presentation.pdf" if kind == "slides" else "paper.pdf",
                    "Idempotency-Key": key,
                    "X-Seminar-Version": session_version(self.row),
                },
            ),
        )
        self.assertEqual(response.status, 201, await response.text())
        return (await response.json())["document"], key, body

    async def test_presenter_context_and_all_owned_sessions_without_other_presenters(self):
        with self.bot.store.db:
            past = self.bot.store.db.execute(
                "INSERT INTO sessions(guild_id,seminar_date,presenter_id) VALUES(1,?,101)",
                ((self.today - timedelta(days=365)).isoformat(),),
            ).lastrowid
        grant = await self.grant()
        response = await self.client.get("/v1/submission/context", headers=self.headers(grant))
        context = await response.json()
        self.assertIn(past, [row["id"] for row in context["sessions"]])
        self.assertGreater(len(context["sessions"]), 1)
        for selected in context["sessions"]:
            self.assertEqual(self.bot.store.session(1, selected["id"])["presenter_id"], 101)
            response = await self.client.get(
                f"/v1/sessions/{selected['id']}", headers=self.headers(grant)
            )
            self.assertEqual(response.status, 200)
        other = self.bot.store.session_on(1, self.today + timedelta(days=7))
        self.assertEqual(
            (
                await self.client.get(f"/v1/sessions/{other['id']}", headers=self.headers(grant))
            ).status,
            403,
        )
        own = self.bot.store.session_on(1, self.today + timedelta(days=14))
        self.assertEqual((await self.paper(grant, session=own)).status, 201)

    async def test_legacy_scope_stays_single_session_and_reissue_revokes_all_presenter_links(self):
        legacy = await self.grant(scope="session")
        own = self.bot.store.session_on(1, self.today + timedelta(days=14))
        self.assertEqual(
            (
                await self.client.get(f"/v1/sessions/{own['id']}", headers=self.headers(legacy))
            ).status,
            403,
        )
        context = await (
            await self.client.get("/v1/submission/context", headers=self.headers(legacy))
        ).json()
        self.assertEqual(len(context["sessions"]), 1)
        newer = await self.grant(session=own)
        self.assertEqual(
            (await self.client.get("/v1/submission/context", headers=self.headers(legacy))).status,
            401,
        )
        self.assertEqual(
            (await self.client.get("/v1/submission/context", headers=self.headers(newer))).status,
            200,
        )
        self.bot.store.swap(
            1, self.today + timedelta(days=14), self.today + timedelta(days=21), self.today
        )
        self.assertEqual(
            (await self.client.get("/v1/submission/context", headers=self.headers(newer))).status,
            401,
        )

    async def test_manual_paper_normalization_search_optional_clear_and_revision_conflicts(self):
        grant = await self.grant()
        fields = {
            "title": "A detailed study",
            "venue": "NeurIPS",
            "year": 2025,
            "doi": "https://doi.org/10.1234/PAPER",
            "link": "https://example.test/paper",
        }
        response = await self.paper(grant, fields)
        self.assertEqual(response.status, 201, await response.text())
        record = (await response.json())["document"]
        self.assertTrue(record["editable"])
        self.assertEqual(
            (record["source"], record["doi"], record["venue"], record["year"]),
            ("manual", "10.1234/paper", "NeurIPS", 2025),
        )
        for query in ("NeurIPS", "2025", "10.1234/paper", "example.test/paper"):
            self.assertEqual(self.bot.store.search(1, query)[1], 1)
        response = await self.mutate(
            grant, record, {"title": "A", "venue": "", "year": "", "doi": None, "link": None}
        )
        self.assertEqual(response.status, 200, await response.text())
        edited = (await response.json())["document"]
        self.assertNotEqual(edited["revision"], record["revision"])
        for name in ("venue", "year", "doi", "link"):
            self.assertIsNone(edited[name])
        stale = await self.mutate(grant, record)
        self.assertEqual((stale.status, (await stale.json())["code"]), (409, "document_changed"))
        self.assertEqual(self.bot.store.search(1, "NeurIPS")[1], 0)
        self.assertEqual(self.bot.store.document(1, record["id"])["file_path"], "")

    async def test_manual_duplicates_short_titles_pdf_titles_and_doi_are_merged(self):
        grant = await self.grant()
        for title in ("A", "a", "Ａ"):
            response = await self.paper(grant, {"title": title})
            self.assertEqual(response.status, 201)
        self.assertEqual(self.api.status(self.row["id"])["papers"], 1)
        pdf, _, _ = await self.uploaded(
            grant, kind="paper", title="A Long Scientific Research Study"
        )
        self.assertEqual(
            (await self.paper(grant, {"title": "A Long Scientific Research Study"})).status, 201
        )
        self.assertEqual(self.api.status(self.row["id"])["papers"], 2)
        changed = await self.mutate(grant, pdf, {"title": "A"})
        self.assertEqual(changed.status, 200)
        self.assertEqual(self.api.status(self.row["id"])["papers"], 2)
        for title in ("Different Title One", "Different Title Two"):
            self.assertEqual(
                (await self.paper(grant, {"title": title, "doi": "doi:10.1234/common"})).status, 201
            )
        self.assertEqual(self.api.status(self.row["id"])["papers"], 3)

    async def test_manual_idempotency_delete_tombstone_and_concurrent_revision_mutation(self):
        grant = await self.grant()
        key = str(uuid.uuid4())
        one = await self.paper(grant, key=key)
        record = (await one.json())["document"]
        retry = await self.paper(grant, key=key)
        self.assertEqual(
            (retry.status, (await retry.json())["document"]["id"]), (200, record["id"])
        )
        mutations = await asyncio.gather(
            self.mutate(grant, record, {"title": "First"}),
            self.mutate(grant, record, {"title": "Second"}),
        )
        self.assertEqual(sorted(response.status for response in mutations), [200, 409])
        current = self.bot.store.document(1, record["id"])
        record["revision"] = document_version(current)
        self.assertEqual((await self.mutate(grant, record, method="delete")).status, 200)
        replay = await self.paper(grant, key=key)
        self.assertEqual((replay.status, (await replay.json())["code"]), (409, "document_changed"))
        self.assertEqual(
            self.bot.store.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0
        )

    async def test_deleted_row_id_reuse_does_not_accept_the_deleted_documents_revision(self):
        grant = await self.grant()
        first = (await (await self.paper(grant)).json())["document"]
        self.assertEqual((await self.mutate(grant, first, method="delete")).status, 200)
        recreated = (await (await self.paper(grant)).json())["document"]
        self.assertEqual(first["id"], recreated["id"])
        self.assertNotEqual(first["revision"], recreated["revision"])
        stale = await self.mutate(grant, first, method="delete")
        self.assertEqual((stale.status, (await stale.json())["code"]), (409, "document_changed"))
        self.assertTrue(self.bot.store.document(1, recreated["id"]))

    async def test_manual_metadata_cannot_be_classified_as_an_original_presentation(self):
        grant = await self.grant()
        record = (await (await self.paper(grant, {"title": "Same"})).json())["document"]
        with self.assertRaisesRegex(ValueError, "발표 자료"):
            self.bot.store.classify(1, record["id"], "slides", 101, False)
        self.assertEqual(self.api.status(self.row["id"])["slides"], 0)

    async def test_revoked_grant_during_metadata_body_read_cannot_mutate_saved_document(self):
        for method in ("PATCH", "DELETE"):
            grant = await self.grant()
            record = (await (await self.paper(grant)).json())["document"]
            original = self.bot.store.document(1, record["id"])
            guarded = asyncio.Event()
            validate = self.api._submission_scope

            def mark_guarded(request, validate=validate, guarded=guarded):
                scope = validate(request)
                guarded.set()
                return scope

            self.api._submission_scope = mark_guarded

            async def body(method=method, guarded=guarded):
                yield b"{"
                await asyncio.wait_for(guarded.wait(), 5)
                self.access.issue(1, self.row["id"], 101, scope="presenter")
                yield b'"title":"Should not change"}' if method == "PATCH" else b"}"

            response = await self.client.request(
                method,
                f"/v1/sessions/{self.row['id']}/documents/{record['id']}",
                data=body(),
                headers=self.headers(
                    grant,
                    **{
                        "Content-Type": "application/json",
                        "X-Seminar-Version": session_version(self.row),
                        "X-Document-Version": record["revision"],
                    },
                ),
            )
            self.assertEqual(response.status, 401)
            self.assertEqual(self.bot.store.document(1, record["id"]), original)
            self.api._submission_scope = validate

    async def test_owner_channel_and_session_authorization_prevents_document_mutation(self):
        grant = await self.grant()
        response = await self.paper(grant)
        record = (await response.json())["document"]
        with self.bot.store.db:
            self.bot.store.db.execute(
                "UPDATE documents SET uploader_id=102 WHERE id=?", (record["id"],)
            )
        detail = await (
            await self.client.get(f"/v1/sessions/{self.row['id']}", headers=self.headers(grant))
        ).json()
        self.assertFalse(detail["documents"][0]["editable"])
        denied = await self.mutate(grant, record)
        self.assertEqual(
            (denied.status, (await denied.json())["code"]), (409, "document_not_editable")
        )
        with self.bot.store.db:
            self.bot.store.db.execute(
                "UPDATE documents SET uploader_id=101,source_channel_id=999 WHERE id=?",
                (record["id"],),
            )
        self.assertEqual((await self.mutate(grant, record)).status, 404)
        self.assertEqual((await self.mutate(grant, record, method="delete")).status, 404)
        with self.bot.store.db:
            self.bot.store.db.execute(
                "UPDATE documents SET source_channel_id=12 WHERE id=?", (record["id"],)
            )
        own = self.bot.store.session_on(1, self.today + timedelta(days=14))
        self.assertEqual((await self.mutate(grant, record, session=own)).status, 404)
        self.assertEqual((await self.mutate(None, record)).status, 401)

    async def test_slides_shared_file_deletion_and_original_upload_retry_after_delete(self):
        grant = await self.grant()
        record, key, body = await self.uploaded(grant)
        stored = self.bot.store.document(1, record["id"])
        path = Path(self.temp.name) / stored["file_path"]
        other, _ = self.bot.store.add_document(
            1,
            999,
            101,
            DocumentInfo("Shared Presentation", "", None, None, stored["sha256"], "filename"),
            "shared.pdf",
            stored["file_path"],
            "https://discord.com/channels/1/12/999",
            datetime.now(UTC),
            kind="slides",
        )
        self.assertEqual((await self.mutate(grant, record, method="delete")).status, 200)
        self.assertTrue(path.exists())
        other_record = self.bot.store.document(1, other)
        shared = self.api.document_data(other_record, {}, 101)
        self.assertEqual((await self.mutate(grant, shared, method="delete")).status, 200)
        self.assertFalse(path.exists())
        response = await self.client.put(
            f"/v1/sessions/{self.row['id']}/documents?kind=slides",
            data=body,
            headers=self.headers(
                grant,
                **{
                    "Content-Type": "application/octet-stream",
                    "X-Filename": "presentation.pdf",
                    "Idempotency-Key": key,
                    "X-Seminar-Version": session_version(self.row),
                },
            ),
        )
        self.assertEqual(response.status, 409)
        self.assertFalse(path.exists())

    async def test_archive_deletion_is_confined_and_failed_database_write_restores_original(self):
        grant = await self.grant()
        record, _, _ = await self.uploaded(grant)
        stored = self.bot.store.document(1, record["id"])
        path = Path(self.temp.name) / stored["file_path"]
        with (
            patch.object(
                self.bot.store, "delete_document", side_effect=RuntimeError("test failure")
            ),
            self.assertLogs("seminar_bot.web_api", level="ERROR"),
        ):
            self.assertEqual((await self.mutate(grant, record, method="delete")).status, 503)
        self.assertTrue(path.exists())
        self.assertTrue(self.bot.store.document(1, record["id"]))
        external = Path(self.temp.name).parent / ("test-external-" + str(uuid.uuid4()))
        external.write_bytes(b"never remove")
        try:
            with self.bot.store.db:
                self.bot.store.db.execute(
                    "UPDATE documents SET file_path=? WHERE id=?", (str(external), record["id"])
                )
            self.assertEqual((await self.mutate(grant, record, method="delete")).status, 503)
            self.assertTrue(external.exists())
        finally:
            external.unlink()

    async def test_validation_blocks_invalid_metadata_and_untrusted_network_destinations(self):
        grant = await self.grant()
        for fields in (
            {"title": ""},
            {"title": "a" * 501},
            {"title": "a\ncontrol"},
            {"title": "\ud800"},
            {"title": "Title", "year": True},
            {"title": "Title", "year": "2025"},
            {"title": "Title", "doi": "invalid"},
            {"title": "Title", "link": "javascript:alert(1)"},
            {"title": "Title", "link": "https://user:pass@example.test/"},
            {"title": "Title", "unexpected": "field"},
        ):
            response = await self.paper(grant, fields)
            self.assertEqual(response.status, 400, await response.text())
        self.assertEqual(
            self.bot.store.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0
        )


class PresenterMigrationTests(unittest.TestCase):
    setUp = fixtures.SubmissionAccessStoreTests.setUp
    tearDown = fixtures.SubmissionAccessStoreTests.tearDown

    def test_v5_grant_keeps_single_session_scope_and_archive_fields_migrate(self):
        record_id, _ = self.store.add_document(
            1,
            123,
            101,
            DocumentInfo("Kept archive", "kept body", None, None, "real-sha", "pdf_metadata"),
            "slides.pdf",
            "files/retained.pdf",
            "https://discord.com/channels/1/12/123",
            fixtures.NOW,
            kind="slides",
        )
        old_archive = {
            key: value
            for key, value in self.store.document(1, record_id).items()
            if key not in {"venue", "year", "link", "edit_revision"}
        }
        invitation, _ = self.access.issue(1, self.row["id"], 101, now=100)
        token, _ = self.access.redeem(invitation, 1, now=101)
        with self.store.db:
            for table in ("submission_invitations", "submission_grants"):
                self.store.db.execute(f"ALTER TABLE {table} DROP COLUMN scope")
            for name in ("venue", "year", "link", "edit_revision"):
                self.store.db.execute(f"ALTER TABLE documents DROP COLUMN {name}")
            self.store.db.execute("PRAGMA user_version=5")
        self.store.close()
        self.store = Store(self.path)
        self.access = SubmissionAccess(self.store)
        grant = self.access.validate(token, 1, now=102)
        self.assertEqual(grant["scope"], "session")
        self.assertEqual(len(self.access.sessions(grant)), 1)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 7)
        upgraded = self.store.document(1, record_id)
        self.assertEqual({key: upgraded[key] for key in old_archive}, old_archive)
        self.store.swap(1, fixtures.DAY, fixtures.DAY + timedelta(days=7), fixtures.DAY)
        with self.assertRaises(SubmissionAccessDenied):
            self.access.validate(token, 1, now=102)


if __name__ == "__main__":
    unittest.main()

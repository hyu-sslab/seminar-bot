from __future__ import annotations

import unittest
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_submission_access as fixtures

from seminar_bot.documents import DocumentInfo
from seminar_bot.store import UserError


class ArchiveStoreTests(unittest.TestCase):
    setUp = fixtures.SubmissionAccessStoreTests.setUp
    tearDown = fixtures.SubmissionAccessStoreTests.tearDown

    def add(
        self,
        index,
        *,
        day=None,
        uploaded=None,
        kind="paper",
        channel=12,
        guild=1,
        title=None,
        doi=None,
        arxiv=None,
        venue=None,
        year=None,
        link=None,
        manual=False,
    ):
        day = day or fixtures.DAY
        row = self.store.session_on(guild, day)
        if row is None:
            with self.store.db:
                self.store.db.execute(
                    "INSERT INTO sessions(guild_id,seminar_date,presenter_id) VALUES(?,?,101)",
                    (guild, day.isoformat()),
                )
            row = self.store.session_on(guild, day)
        record_id, _ = self.store.add_document(
            guild,
            index,
            row["presenter_id"],
            DocumentInfo(
                title or f"Scientific Study Number {index}",
                "private body" if kind == "slides" else "",
                doi,
                arxiv,
                f"sha-{index}",
                "manual" if manual else "pdf_metadata",
            ),
            "manual-paper.pdf" if manual else f"document-{index}.pdf",
            "" if kind != "slides" else f"files/{index}.pdf",
            "https://site.test/internal/seminars/",
            uploaded or fixtures.NOW,
            source_channel_id=channel,
            kind=kind,
            session_id=row["id"],
            source_type="manual" if manual else "web",
            upload_key=str(uuid.uuid4()),
            venue=venue,
            year=year,
            link=link,
        )
        return record_id

    def archive(self, **kwargs):
        return self.store.archive(1, visible_channel_ids=[12], **kwargs)

    def test_archive_is_document_feed_sorted_by_seminar_then_upload_then_id(self):
        before = self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(self.archive(), ([], 0))
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], before
        )
        older = self.add(
            1,
            day=fixtures.DAY - timedelta(days=7),
            uploaded=fixtures.NOW + timedelta(days=30),
            kind="slides",
        )
        first = self.add(2, uploaded=fixtures.NOW + timedelta(hours=1))
        second = self.add(3, uploaded=fixtures.NOW + timedelta(hours=2))
        tie = self.add(4, uploaded=fixtures.NOW + timedelta(hours=2))
        unlinked = self.add(5, uploaded=fixtures.NOW + timedelta(days=1))
        with self.store.db:
            self.store.db.execute("UPDATE documents SET session_id=NULL WHERE id=?", (unlinked,))
        rows, total = self.archive()
        self.assertEqual(total, 5)
        self.assertEqual([row["id"] for row in rows], [unlinked, tie, second, first, older])
        self.assertIsNone(rows[0]["seminar_date"])
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], before + 1
        )

    def test_archive_filters_guild_channel_kind_and_includes_manual_and_unknown(self):
        slides = self.add(1, kind="slides")
        manual = self.add(2, manual=True)
        unknown = self.add(3, kind="unknown")
        self.add(4, channel=99)
        self.store.configure(
            2, 21, 12, fixtures.DAY.weekday(), "09:00", "18:00", "UTC", fixtures.NOW
        )
        self.add(5, guild=2)
        rows, total = self.archive()
        self.assertEqual((total, {row["id"] for row in rows}), (3, {slides, manual, unknown}))
        self.assertEqual([row["id"] for row in self.archive(kind="slides")[0]], [slides])
        self.assertEqual([row["id"] for row in self.archive(kind="paper")[0]], [manual])
        self.assertEqual(self.store.archive(1, visible_channel_ids=[]), ([], 0))

    def test_archive_search_normalizes_titles_doi_arxiv_and_manual_metadata(self):
        expected = self.add(
            1,
            title="한글 Ａttention Study",
            doi="10.1234/test",
            arxiv="2301.12345",
            manual=True,
            venue="ICLR",
            year=2025,
            link="https://example.test/paper",
        )
        self.add(2, title="Unrelated Results")
        for query in (
            "한글attention",
            "https://doi.org/10.1234/test",
            "DOI:10.1234/test",
            "arXiv:2301.12345v3",
            "https://arxiv.org/pdf/2301.12345v2.pdf",
            "ICLR",
            "2025",
            "example.test/paper",
        ):
            rows, total = self.archive(q=query)
            self.assertEqual((total, [row["id"] for row in rows]), (1, [expected]), query)
        self.assertEqual(self.archive(q="' OR 1=1 --"), ([], 0))
        self.assertEqual(self.archive(q="NotInArchive"), ([], 0))
        self.assertEqual(self.archive(q="   ")[1], 2)

    def test_archive_pagination_is_stable_and_out_of_range_is_empty_with_total(self):
        for index in range(1, 46):
            self.add(index)
        one, total = self.archive(page=1)
        two, _ = self.archive(page=2)
        three, _ = self.archive(page=3)
        self.assertEqual((len(one), len(two), len(three), total), (20, 20, 5, 45))
        self.assertEqual([row["id"] for row in one + two + three], list(range(45, 0, -1)))
        self.assertEqual(self.archive(page=4), ([], 45))

    def test_archive_rejects_invalid_limits_and_search_without_treating_it_as_all(self):
        for args in (
            {"q": "x" * 201},
            {"q": "가" * 134},
            {"q": "bad\x00query"},
            {"q": "\ud800"},
            {"q": "%%%"},
            {"page": 0},
            {"page": 100001},
            {"page_size": 0},
            {"kind": "unknown"},
        ):
            with self.assertRaises(UserError):
                self.archive(**args)


class ArchiveAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.SubmissionAccessAPITests.asyncSetUp
    asyncTearDown = fixtures.SubmissionAccessAPITests.asyncTearDown
    headers = fixtures.SubmissionAccessAPITests.headers

    def add(self, index, *, channel=12, kind="paper", manual=False):
        record_id, _ = self.bot.store.add_document(
            1,
            index,
            101,
            DocumentInfo(
                f"Scientific Results {index}",
                "private extracted body" if kind == "slides" else "",
                f"10.1234/paper{index}",
                None,
                f"secret-sha-{index}",
                "manual" if manual else "pdf_metadata",
            ),
            "manual-paper.pdf" if manual else f"document-{index}.pdf",
            "" if kind != "slides" else f"files/private-path-{index}.pdf",
            "https://site.test/internal/seminars/",
            datetime.now(UTC),
            source_channel_id=channel,
            kind=kind,
            session_id=self.row["id"],
            source_type="manual" if manual else "web",
            upload_key=str(uuid.uuid4()),
            venue="ICML" if manual else None,
            year=2025 if manual else None,
        )
        return record_id

    async def test_archive_requires_existing_lab_server_auth_and_denies_submission_capabilities(
        self,
    ):
        self.add(1)
        self.assertEqual((await self.client.get("/v1/archive")).status, 401)
        invitation, _ = self.access.issue(1, self.row["id"], 101, scope="presenter")
        grant, _ = self.access.redeem(invitation, 1)
        self.assertEqual(
            (await self.client.get("/v1/archive", headers=self.headers(grant))).status, 403
        )
        self.assertEqual(
            (await self.client.get("/v1/archive", headers=self.headers(""))).status, 401
        )
        self.bot.is_ready.return_value = False
        self.assertEqual((await self.client.get("/v1/archive", headers=self.headers())).status, 503)

    async def test_archive_returns_safe_metadata_and_scopes_current_channel(self):
        manual = self.add(1, manual=True)
        slide = self.add(2, kind="slides")
        self.add(3, channel=99)
        response = await self.client.get("/v1/archive?kind=all", headers=self.headers())
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual((data["total"], data["page"], data["page_size"]), (2, 1, 20))
        self.assertEqual({row["id"] for row in data["documents"]}, {manual, slide})
        for row in data["documents"]:
            self.assertIn("uploaded_at", row)
            self.assertFalse(row["editable"])
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        for secret in (
            "secret-sha",
            "private-path",
            "private extracted body",
            "upload_key",
            "token_hash",
            "source_channel_id",
        ):
            self.assertNotIn(secret, await response.text())
        response = await self.client.get("/v1/archive?kind=paper&q=ICML", headers=self.headers())
        self.assertEqual([row["id"] for row in (await response.json())["documents"]], [manual])

    async def test_presenter_name_uses_rotation_then_discord_then_id_and_keeps_unlinked_blank(self):
        record_id = self.add(1)

        async def name():
            response = await self.client.get("/v1/archive", headers=self.headers())
            return (await response.json())["documents"][0]["presenter_name"]

        self.bot.get_guild = Mock(
            return_value=SimpleNamespace(
                get_member=lambda _id: SimpleNamespace(display_name="Discord Display")
            )
        )
        with patch.object(self.api, "names", AsyncMock(return_value={101: "Rotation Display"})):
            self.assertEqual(await name(), "Rotation Display")
        with patch.object(self.api, "names", AsyncMock(return_value={})):
            self.assertEqual(await name(), "Discord Display")
            self.bot.get_guild.return_value = None
            self.assertEqual(await name(), "발표자 101")
            with self.bot.store.db:
                self.bot.store.db.execute(
                    "UPDATE documents SET session_id=NULL WHERE id=?", (record_id,)
                )
            self.bot.get_guild.reset_mock()
            self.assertEqual(await name(), "")
            self.bot.get_guild.assert_not_called()

    async def test_archive_fixed_page_size_and_query_validation_leave_search_contract_intact(self):
        for index in range(1, 24):
            self.add(index)
        archive = await (await self.client.get("/v1/archive?page=2", headers=self.headers())).json()
        self.assertEqual(
            (len(archive["documents"]), archive["total"], archive["page_size"]), (3, 23, 20)
        )
        search = await (
            await self.client.get("/v1/search?q=Scientific", headers=self.headers())
        ).json()
        self.assertEqual((len(search["documents"]), search["total"]), (10, 23))
        self.assertNotIn("page_size", search)
        for query in (
            "page=0",
            "page=01",
            "page=100001",
            "page=1&page=2",
            "kind=unknown",
            "kind=all&kind=paper",
            "page_size=1",
            "q=one&q=two",
            "q=" + "x" * 201,
            "q=" + "가" * 134,
            "q=%00",
            "q=%25%25%25",
        ):
            response = await self.client.get("/v1/archive?" + query, headers=self.headers())
            self.assertEqual(response.status, 400, query)
        self.assertEqual(
            (await self.client.get("/v1/archive?q=", headers=self.headers())).status, 200
        )


if __name__ == "__main__":
    unittest.main()

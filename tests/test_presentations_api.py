from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import test_archive_api as archive_fixtures
import test_submission_access as fixtures

from seminar_bot.store import UserError


class PresentationsStoreTests(unittest.TestCase):
    setUp = fixtures.SubmissionAccessStoreTests.setUp
    tearDown = fixtures.SubmissionAccessStoreTests.tearDown
    add = archive_fixtures.ArchiveStoreTests.add

    def presentations(self, **kwargs):
        return self.store.presentations(1, through=fixtures.DAY, visible_channel_ids=[12], **kwargs)

    def test_includes_existing_old_incomplete_presentations_without_creating_schedule_rows(self):
        old_day = fixtures.DAY - timedelta(days=400)
        old = self.add(1, day=old_day, kind="slides")
        current = self.add(2, manual=True)
        unknown = self.add(3, kind="unknown")
        self.add(4, day=fixtures.DAY + timedelta(days=7), kind="slides")
        unlinked = self.add(5)
        with self.store.db:
            self.store.db.execute("UPDATE documents SET session_id=NULL WHERE id=?", (unlinked,))
        before_config = self.store.require_config(1)
        before_count = self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        rows, total = self.presentations()
        self.assertEqual(total, 2)
        self.assertEqual(
            [row["session"]["seminar_date"] for row in rows],
            [fixtures.DAY.isoformat(), old_day.isoformat()],
        )
        self.assertEqual(
            [{document["id"] for document in row["documents"]} for row in rows],
            [{current, unknown}, {old}],
        )
        self.assertFalse(self.store.submission(1, self.row["id"])["complete"])
        self.assertEqual(self.store.require_config(1), before_config)
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], before_count
        )
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM web_submission_receipts").fetchone()[0], 0
        )

    def test_pages_whole_sessions_and_returns_all_their_documents(self):
        expected = []
        for index in range(23):
            day = fixtures.DAY - timedelta(days=index * 7)
            ids = {
                self.add(index * 4 + offset, day=day, kind="slides" if offset == 1 else "paper")
                for offset in range(1, 5)
            }
            expected.append(ids)
        pages = [self.presentations(page=page) for page in (1, 2, 3)]
        self.assertEqual([len(rows) for rows, _ in pages], [10, 10, 3])
        self.assertEqual([total for _, total in pages], [23, 23, 23])
        groups = [
            {document["id"] for document in row["documents"]} for rows, _ in pages for row in rows
        ]
        self.assertEqual(groups, expected)
        self.assertEqual(self.presentations(page=4), ([], 23))

    def test_only_current_channel_and_guild_can_make_a_session_visible_or_supply_documents(self):
        visible = self.add(1, kind="slides")
        self.add(2, channel=99)
        self.add(3, day=fixtures.DAY - timedelta(days=7), channel=99)
        self.store.configure(
            2, 21, 12, fixtures.DAY.weekday(), "09:00", "18:00", "UTC", fixtures.NOW
        )
        self.add(4, guild=2)
        # Even a malformed cross-guild session reference must not expose that file.
        other = self.add(5, guild=2)
        with self.store.db:
            self.store.db.execute(
                "UPDATE documents SET session_id=? WHERE id=?", (self.row["id"], other)
            )
        rows, total = self.presentations()
        self.assertEqual(total, 1)
        self.assertEqual([document["id"] for document in rows[0]["documents"]], [visible])
        self.assertEqual(self.store.presentations(1, fixtures.DAY, visible_channel_ids=[]), ([], 0))

    def test_matching_one_paper_or_presenter_returns_the_entire_matching_session(self):
        slide = self.add(1, kind="slides", title="Presentation Slides")
        paper = self.add(
            2,
            title="한글 Ａttention Study",
            doi="10.1234/test",
            arxiv="2301.12345",
            venue="ICLR",
            year=2025,
            manual=True,
        )
        self.add(3, day=fixtures.DAY - timedelta(days=7), title="Unrelated Results")
        for query in (
            "한글attention",
            "https://doi.org/10.1234/test",
            "arXiv:2301.12345v3",
            "ICLR",
            "2025",
        ):
            rows, total = self.presentations(q=query)
            self.assertEqual(total, 1, query)
            self.assertEqual({document["id"] for document in rows[0]["documents"]}, {slide, paper})
        rows, total = self.presentations(q="발표자 이름", presenter_ids=[101])
        self.assertEqual(total, 2)
        self.assertEqual({document["id"] for document in rows[0]["documents"]}, {slide, paper})
        self.assertEqual(self.presentations(q="' OR 1=1 --"), ([], 0))
        self.assertEqual(self.presentations(q="NoResults"), ([], 0))
        self.assertEqual(self.presentations(q="  ")[1], 2)

    def test_rejects_invalid_limits_and_queries(self):
        for kwargs in (
            {"page": 0},
            {"page": 100001},
            {"page": True},
            {"page_size": 0},
            {"q": "x" * 201},
            {"q": "가" * 134},
            {"q": "bad\x00query"},
            {"q": "\ud800"},
            {"q": "%%%"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(UserError):
                self.presentations(**kwargs)


class PresentationsAPITests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.SubmissionAccessAPITests.asyncTearDown
    headers = fixtures.SubmissionAccessAPITests.headers

    async def asyncSetUp(self):
        await fixtures.SubmissionAccessAPITests.asyncSetUp(self)
        self.store = self.bot.store

    def add(self, index, *, day=None, **kwargs):
        return archive_fixtures.ArchiveStoreTests.add(self, index, day=day or self.today, **kwargs)

    async def test_requires_existing_lab_auth_and_rejects_submission_capabilities(self):
        self.add(1)
        self.assertEqual((await self.client.get("/v1/presentations")).status, 401)
        token, _ = self.access.issue(1, self.row["id"], 101, scope="presenter")
        grant, _ = self.access.redeem(token, 1)
        self.assertEqual(
            (await self.client.get("/v1/presentations", headers=self.headers(grant))).status, 403
        )
        self.bot.is_ready.return_value = False
        self.assertEqual(
            (await self.client.get("/v1/presentations", headers=self.headers())).status, 503
        )

    async def test_returns_safe_complete_groups_in_date_order_even_without_submission_receipt(self):
        slides = self.add(1, kind="slides")
        papers = {self.add(index, manual=True) for index in (2, 3, 4)}
        old = self.add(5, day=self.today - timedelta(days=400), kind="slides")
        self.add(6, channel=99)
        self.add(7, day=self.today + timedelta(days=7))
        response = await self.client.get("/v1/presentations", headers=self.headers())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        data = await response.json()
        self.assertEqual((data["total"], data["page"], data["page_size"]), (2, 1, 10))
        first, second = data["presentations"]
        self.assertEqual(first["session"]["id"], self.row["id"])
        self.assertEqual(first["session"]["presenter_name"], "발표자 101")
        self.assertEqual((first["slides_count"], first["paper_count"]), (1, 3))
        self.assertEqual({row["id"] for row in first["documents"]}, {slides, *papers})
        self.assertEqual([row["id"] for row in second["documents"]], [old])
        self.assertTrue(all(not row["editable"] for row in first["documents"]))
        for secret in ("sha-", "private body", "file_path", "upload_key", "source_channel_id"):
            self.assertNotIn(secret, await response.text())
        self.bot.notify_web_submission.assert_not_called()

    async def test_searches_rotation_and_cached_discord_names_and_materials_without_filtering_group(
        self,
    ):
        slides = self.add(1, kind="slides", title="Talk Slides")
        paper = self.add(2, title="Target Research", venue="ICML", year=2025)
        self.bot.get_guild = Mock(
            return_value=SimpleNamespace(
                members=[SimpleNamespace(id=101, display_name="Cached Name")],
                get_member=lambda _id: SimpleNamespace(display_name="Cached Name"),
            )
        )
        with patch.object(self.api, "names", AsyncMock(return_value={101: "Rotation Name"})):
            for query in ("rotationname", "Target", "ICML", "2025"):
                response = await self.client.get(
                    "/v1/presentations", params={"q": query}, headers=self.headers()
                )
                data = await response.json()
                self.assertEqual(data["total"], 1, query)
                self.assertEqual(
                    {row["id"] for row in data["presentations"][0]["documents"]}, {slides, paper}
                )
                self.assertEqual(
                    data["presentations"][0]["session"]["presenter_name"], "Rotation Name"
                )
        with patch.object(self.api, "names", AsyncMock(return_value={})):
            response = await self.client.get(
                "/v1/presentations?q=cachedname", headers=self.headers()
            )
            self.assertEqual((await response.json())["total"], 1)

    async def test_uses_configured_timezone_for_future_exclusion(self):
        tomorrow = self.today + timedelta(days=1)
        self.add(1, day=tomorrow, kind="slides")
        with self.store.db:
            self.store.db.execute("UPDATE guilds SET timezone='Asia/Seoul' WHERE guild_id=1")
        with patch("seminar_bot.store.datetime") as clock:
            clock.now.return_value = datetime.combine(self.today, datetime.min.time(), UTC).replace(
                hour=23, minute=30
            )
            response = await self.client.get("/v1/presentations", headers=self.headers())
        data = await response.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["presentations"][0]["session"]["seminar_date"], tomorrow.isoformat())

    async def test_fixed_session_page_size_and_strict_query_validation(self):
        for index in range(13):
            self.add(index + 1, day=self.today - timedelta(days=index * 7), kind="slides")
        response = await self.client.get("/v1/presentations?page=2", headers=self.headers())
        data = await response.json()
        self.assertEqual(
            (len(data["presentations"]), data["total"], data["page_size"]), (3, 13, 10)
        )
        for query in (
            "page=0",
            "page=01",
            "page=100001",
            "page=1&page=2",
            "kind=slides",
            "page_size=1",
            "q=one&q=two",
            "q=" + "x" * 201,
            "q=" + "가" * 134,
            "q=%00",
            "q=%25%25%25",
        ):
            response = await self.client.get("/v1/presentations?" + query, headers=self.headers())
            self.assertEqual(response.status, 400, query)
        self.assertEqual(
            (await self.client.get("/v1/presentations?q=", headers=self.headers())).status, 200
        )


if __name__ == "__main__":
    unittest.main()

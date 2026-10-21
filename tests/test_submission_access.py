from __future__ import annotations

import asyncio
import hashlib
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import urlsplit

from aiohttp.test_utils import TestClient, TestServer

from seminar_bot.bot import SeminarBot
from seminar_bot.documents import DocumentInfo
from seminar_bot.settings import Settings
from seminar_bot.store import Store, UserError
from seminar_bot.submission_access import (
    GRANT_TTL,
    INVITATION_TTL,
    SubmissionAccess,
    SubmissionAccessDenied,
    session_version,
    submission_link,
)
from seminar_bot.web_api import SeminarAPI

DAY = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, tzinfo=UTC)


class SubmissionAccessStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "store.sqlite3"
        self.store = Store(self.path)
        self.store.configure(1, 11, 12, DAY.weekday(), "09:00", "18:00", "UTC", NOW)
        self.store.set_rotation(1, [101, 102], DAY, DAY)
        self.row = self.store.session_on(1, DAY)
        self.access = SubmissionAccess(self.store)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def issue(self):
        return self.access.issue(1, self.row["id"], 101, now=100)

    def test_hash_only_storage_one_time_redemption_and_expiry_boundaries(self):
        invitation, expires = self.issue()
        self.assertEqual((len(invitation), expires), (43, 100 + INVITATION_TTL))
        grant, grant_expires = self.access.redeem(invitation, 1, now=101)
        self.assertEqual(grant_expires, 101 + GRANT_TTL)
        scope = self.access.validate(grant, 1, now=grant_expires - 1)
        self.assertEqual(scope["session_id"], self.row["id"])
        self.assertEqual(scope["presenter_id"], 101)
        with self.assertRaises(SubmissionAccessDenied):
            self.access.validate(grant, 1, now=grant_expires)
        with self.assertRaises(SubmissionAccessDenied):
            self.access.redeem(invitation, 1, now=102)
        database = "\n".join(self.store.db.iterdump())
        self.assertNotIn(invitation, database)
        self.assertNotIn(grant, database)

    def test_expired_malformed_forged_and_cross_guild_tokens_fail_closed(self):
        invitation, expires = self.issue()
        for token in ("", "x" * 42, "x" * 44, "!" * 43, None, {}, "x" * 43):
            with self.assertRaises(SubmissionAccessDenied):
                self.access.redeem(token, 1, now=101)
        with self.assertRaises(SubmissionAccessDenied):
            self.access.redeem(invitation, 2, now=101)
        with self.assertRaises(SubmissionAccessDenied):
            self.access.redeem(invitation, 1, now=expires)
        grant, _ = self.access.redeem(invitation, 1, now=101)
        with self.assertRaises(SubmissionAccessDenied):
            self.access.validate(grant, 2, now=102)
        with self.assertRaises(UserError):
            self.access.issue(1, self.row["id"], 102)

    def test_concurrent_redemption_from_separate_sqlite_connections_has_one_winner(self):
        invitation, _ = self.issue()
        barrier = threading.Barrier(2)
        initialize = threading.Lock()

        def redeem():
            # Production migrates once under the process instance lock. Keep
            # migration writes out of this test's redemption transaction race,
            # while each SQLite connection remains owned by its worker thread.
            with initialize:
                store = Store(self.path)
                self.assertFalse(store.db.in_transaction)
            try:
                barrier.wait(timeout=5)
                try:
                    SubmissionAccess(store).redeem(invitation, 1, now=101)
                    return True
                except SubmissionAccessDenied:
                    return False
            finally:
                store.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            one, two = executor.submit(redeem), executor.submit(redeem)
            self.assertEqual(
                sorted([one.result(timeout=10), two.result(timeout=10)]), [False, True]
            )
        self.assertEqual(
            self.store.db.execute("SELECT COUNT(*) FROM submission_grants").fetchone()[0], 1
        )

    def test_new_link_revokes_previous_links_and_browser_grants(self):
        first, _ = self.issue()
        self.issue()
        with self.assertRaises(SubmissionAccessDenied):
            self.access.redeem(first, 1, now=101)
        current, _ = self.issue()
        grant, _ = self.access.redeem(current, 1, now=101)
        self.issue()
        with self.assertRaises(SubmissionAccessDenied):
            self.access.validate(grant, 1, now=102)

    def test_swapping_back_or_upload_channel_change_never_revives_old_access(self):
        invitation, _ = self.issue()
        grant, _ = self.access.redeem(invitation, 1, now=101)
        self.store.swap(1, DAY, DAY + timedelta(days=7), DAY)
        self.store.swap(1, DAY, DAY + timedelta(days=7), DAY)
        with self.assertRaises(SubmissionAccessDenied):
            self.access.validate(grant, 1, now=102)
        invitation, _ = self.issue()
        self.store.configure(1, 11, 13, DAY.weekday(), "09:00", "18:00", "UTC", NOW)
        self.store.configure(1, 11, 12, DAY.weekday(), "09:00", "18:00", "UTC", NOW)
        with self.assertRaises(SubmissionAccessDenied):
            self.access.redeem(invitation, 1, now=101)

    def test_postponement_and_deleted_recreated_session_revoke_invites(self):
        invitation, _ = self.issue()
        self.store.postpone(1, DAY)
        with self.assertRaises(SubmissionAccessDenied):
            self.access.redeem(invitation, 1, now=101)
        self.row = self.store.session(1, self.row["id"])
        invitation, _ = self.issue()
        self.store.set_rotation(1, [101, 102], DAY, DAY)
        with self.assertRaises(SubmissionAccessDenied):
            self.access.redeem(invitation, 1, now=101)

    def test_scope_checks_current_presenter_date_and_channel_even_without_revocation(self):
        for sql, value in (
            ("UPDATE sessions SET presenter_id=? WHERE id=?", 999),
            ("UPDATE sessions SET seminar_date=? WHERE id=?", "2026-10-23"),
            ("UPDATE guilds SET upload_channel_id=? WHERE guild_id=?", 99),
        ):
            invitation, _ = self.issue()
            with self.store.db:
                self.store.db.execute(sql, (value, self.row["id"] if "sessions" in sql else 1))
            with self.assertRaises(SubmissionAccessDenied):
                self.access.redeem(invitation, 1, now=101)
            with self.store.db:
                self.store.db.execute(
                    "UPDATE sessions SET presenter_id=101, seminar_date=? WHERE id=?",
                    (DAY.isoformat(), self.row["id"]),
                )
                self.store.db.execute("UPDATE guilds SET upload_channel_id=12 WHERE guild_id=1")

    def test_session_selection_is_owned_recent_bounded_and_supports_explicit_date(self):
        self.assertEqual(self.access.select_session(1, 102, DAY)["seminar_date"], "2026-10-29")
        self.assertEqual(
            self.access.select_session(1, 101, DAY + timedelta(days=1))["id"], self.row["id"]
        )
        self.assertNotEqual(
            self.access.select_session(1, 101, DAY + timedelta(days=29))["id"], self.row["id"]
        )
        with self.assertRaises(UserError):
            self.access.select_session(1, 102, DAY, DAY)
        with self.assertRaises(UserError):
            self.access.select_session(1, 999, DAY)
        with patch.object(self.store, "submission", return_value={"complete": True}):
            selected = self.access.select_session(1, 101, DAY + timedelta(days=1))
            self.assertEqual(selected["seminar_date"], "2026-11-05")

    def test_link_secret_only_appears_in_fragment(self):
        url = urlsplit(
            submission_link("https://site.test/internal/seminars/?ignored=yes", "x" * 43)
        )
        self.assertEqual((url.path, url.query, url.fragment), ("/submit/", "", "token=" + "x" * 43))
        with self.assertRaises(UserError):
            submission_link("http://site.test/", "x" * 43)

    def test_v4_upgrade_adds_capability_tables_without_changing_existing_archive(self):
        record_id, _ = self.store.add_document(
            1,
            123,
            101,
            DocumentInfo(
                "Existing Presentation", "body", None, None, "archive-sha", "pdf_metadata"
            ),
            "presentation.pdf",
            "files/retained.pdf",
            "https://discord.com/channels/1/12/123",
            NOW,
            kind="slides",
            session_id=self.row["id"],
        )
        original = self.store.document(1, record_id)
        with self.store.db:
            self.store.db.execute("DROP TABLE submission_grants")
            self.store.db.execute("DROP TABLE submission_invitations")
            self.store.db.execute("PRAGMA user_version=4")
        self.store.close()
        self.store = Store(self.path)
        self.access = SubmissionAccess(self.store)
        self.assertEqual(self.store.document(1, record_id), original)
        self.assertEqual(self.store.session(1, self.row["id"]), self.row)
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0], 5)
        invitation, _ = self.issue()
        grant, _ = self.access.redeem(invitation, 1, now=101)
        self.assertEqual(self.access.validate(grant, 1, now=102)["session_id"], self.row["id"])


class SubmissionAccessAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.today = datetime.now(UTC).date()
        self.bot = SeminarBot(
            Settings(
                token="test-discord-token",
                data_dir=Path(self.temp.name),
                rotation_file=Path(self.temp.name) / "rotation.json",
                api_token="a" * 64,
                api_guild_id=1,
                seminar_page_url="https://site.test/internal/seminars/",
                parser_url="http://document-parser:8081",
                api_transport="relay",
                relay_token="b" * 64,
            )
        )
        self.bot.is_ready = Mock(return_value=True)
        self.bot.reload_rotation_file = AsyncMock()
        self.bot.get_guild = Mock(return_value=None)
        self.bot.notify_web_submission = AsyncMock(return_value=123)
        self.bot.store.configure(
            1, 11, 12, self.today.weekday(), "09:00", "18:00", "UTC", datetime.now(UTC)
        )
        self.bot.store.set_rotation(1, [101, 102], self.today, self.today)
        self.row = self.bot.store.session_on(1, self.today)
        self.api = SeminarAPI(self.bot)
        self.access = self.api.submission_access
        self.client = TestClient(TestServer(self.api.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.bot.close()
        self.temp.cleanup()

    def headers(self, grant=None, **extra):
        result = {"Authorization": "Bearer " + "a" * 64, "X-Seminar-Relay-Token": "b" * 64, **extra}
        if grant is not None:
            result["X-Seminar-Submission-Token"] = grant
        return result

    async def grant(self):
        token, _ = self.access.issue(1, self.row["id"], 101)
        response = await self.client.post(
            "/v1/submission/redeem", json={"token": token}, headers=self.headers()
        )
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())["access_token"]

    async def upload(self, grant):
        return await self.client.put(
            f"/v1/sessions/{self.row['id']}/documents?kind=slides",
            data=b"%PDF-test",
            headers=self.headers(
                grant,
                **{
                    "Content-Type": "application/octet-stream",
                    "X-Filename": "slides.pdf",
                    "Idempotency-Key": str(uuid.uuid4()),
                    "X-Seminar-Version": session_version(self.row),
                },
            ),
        )

    async def test_redeem_and_context_keep_server_auth_and_one_time_rules(self):
        token, _ = self.access.issue(1, self.row["id"], 101)
        response = await self.client.post("/v1/submission/redeem", json={"token": token})
        self.assertEqual(response.status, 401)
        response = await self.client.post(
            "/v1/submission/redeem", json={"token": token}, headers=self.headers()
        )
        data = await response.json()
        self.assertEqual(set(data), {"access_token", "expires_at"})
        grant = data["access_token"]
        response = await self.client.post(
            "/v1/submission/redeem", json={"token": token}, headers=self.headers()
        )
        self.assertEqual(response.status, 401)
        context = await self.client.get("/v1/submission/context", headers=self.headers(grant))
        self.assertEqual(
            await context.json(),
            {
                "session_id": self.row["id"],
                "version": session_version(self.row),
                "expires_at": data["expires_at"],
            },
        )
        self.assertEqual(context.headers["Cache-Control"], "private, no-store")
        self.assertEqual(
            (await self.client.get("/v1/submission/context", headers=self.headers())).status, 401
        )

    async def test_scoped_access_cannot_list_search_download_redeem_or_choose_other_session(self):
        grant = await self.grant()
        own = f"/v1/sessions/{self.row['id']}"
        self.assertEqual((await self.client.get(own, headers=self.headers(grant))).status, 200)
        other = self.bot.store.session_on(1, self.today + timedelta(days=7))
        for path, method in (
            ("/v1/sessions", "get"),
            ("/v1/search?q=research", "get"),
            ("/v1/documents/1/file", "get"),
            (f"/v1/sessions/{other['id']}", "get"),
            (f"/v1/sessions/{other['id']}/documents", "put"),
            (f"/v1/sessions/{other['id']}/submit", "post"),
            ("/v1/submission/redeem", "post"),
        ):
            response = await getattr(self.client, method)(path, headers=self.headers(grant))
            self.assertEqual(response.status, 403, path)
        # Established lab website authorization remains unchanged.
        self.assertEqual(
            (await self.client.get("/v1/sessions", headers=self.headers())).status, 200
        )
        for invalid in ("", "x" * 42, "x" * 43):
            self.assertEqual(
                (await self.client.get("/v1/sessions", headers=self.headers(invalid))).status, 401
            )

    async def test_redeem_rejects_large_and_invalid_bodies_without_identity_disclosure(self):
        for body in (
            "null",
            "[]",
            "{}",
            '{"token": 123}',
            "{",
            '{"token":"' + "x" * 43 + '","user_id":101}',
        ):
            response = await self.client.post(
                "/v1/submission/redeem",
                data=body,
                headers=self.headers(**{"Content-Type": "application/json"}),
            )
            self.assertEqual(response.status, 401)
            self.assertNotIn("101", await response.text())
        response = await self.client.post(
            "/v1/submission/redeem",
            data="x" * 1025,
            headers=self.headers(**{"Content-Type": "application/json"}),
        )
        self.assertEqual(response.status, 413)

    async def test_revoked_grant_during_parser_does_not_commit_or_leave_original(self):
        grant = await self.grant()

        async def revoke(path, filename, data):
            self.access.issue(1, self.row["id"], 101)
            return DocumentInfo(
                "Presentation", "", None, None, hashlib.sha256(data).hexdigest(), "filename"
            )

        self.bot.extract_document = AsyncMock(side_effect=revoke)
        response = await self.upload(grant)
        self.assertEqual(response.status, 401, await response.text())
        self.assertEqual(
            self.bot.store.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0], 0
        )
        self.assertEqual(
            [p for p in (Path(self.temp.name) / "files").rglob("*") if p.is_file()], []
        )

    async def test_owned_uploads_and_completed_submit_retries_preserve_scope_and_retention(self):
        grant = await self.grant()

        async def extract(path, filename, data):
            return DocumentInfo(
                filename.removesuffix(".pdf"),
                "temporary body",
                None,
                None,
                hashlib.sha256(data).hexdigest(),
                "pdf_metadata",
            )

        self.bot.extract_document = AsyncMock(side_effect=extract)
        for index, kind in enumerate(("slides", "paper", "paper", "paper")):
            response = await self.client.put(
                f"/v1/sessions/{self.row['id']}/documents?kind={kind}",
                data=b"%PDF-" + bytes([index]),
                headers=self.headers(
                    grant,
                    **{
                        "Content-Type": "application/octet-stream",
                        "X-Filename": f"Distinct-Research-Study-{index}.pdf",
                        "Idempotency-Key": str(uuid.uuid4()),
                        "X-Seminar-Version": session_version(self.row),
                    },
                ),
            )
            self.assertEqual(response.status, 201, await response.text())
        for _ in range(2):
            response = await self.client.post(
                f"/v1/sessions/{self.row['id']}/submit",
                json={},
                headers=self.headers(grant, **{"X-Seminar-Version": session_version(self.row)}),
            )
            self.assertEqual(response.status, 200, await response.text())
            self.assertTrue((await response.json())["submission"]["complete"])
        self.bot.notify_web_submission.assert_awaited_once()
        records = self.bot.store.db.execute("SELECT * FROM documents ORDER BY id").fetchall()
        self.assertTrue(records[0]["file_path"])
        for record in records[1:]:
            self.assertEqual((record["file_path"], record["extracted_text"]), ("", ""))
        self.assertEqual(self.access.validate(grant, 1)["session_id"], self.row["id"])

    async def test_submit_checks_grant_again_after_reading_body(self):
        grant = await self.grant()
        guarded = asyncio.Event()
        validate = self.api._submission_scope

        def mark_guarded(request):
            scope = validate(request)
            guarded.set()
            return scope

        self.api._submission_scope = mark_guarded

        async def body():
            yield b"{"
            await asyncio.wait_for(guarded.wait(), 5)
            self.access.issue(1, self.row["id"], 101)
            yield b"}"

        response = await self.client.post(
            f"/v1/sessions/{self.row['id']}/submit",
            data=body(),
            headers=self.headers(grant, **{"X-Seminar-Version": session_version(self.row)}),
        )
        self.assertEqual(response.status, 401)
        self.bot.notify_web_submission.assert_not_awaited()

    async def test_discord_command_is_ephemeral_owned_and_has_no_url_token_query(self):
        interaction = SimpleNamespace(
            guild_id=1,
            user=SimpleNamespace(id=101),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        command = self.bot.tree.get_command("제출")
        await command.callback(interaction)
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        sent = interaction.followup.send.await_args
        self.assertTrue(sent.kwargs["ephemeral"])
        url = urlsplit(sent.kwargs["view"].children[0].url)
        self.assertEqual((url.path, url.query), ("/submit/", ""))
        token = url.fragment.removeprefix("token=")
        access, _ = self.access.redeem(token, 1)
        self.assertEqual(self.access.validate(access, 1)["presenter_id"], 101)
        with self.assertRaises(UserError):
            await command.callback(interaction, (self.today + timedelta(days=7)).isoformat())
        interaction.guild_id = 2
        with self.assertRaises(UserError):
            await command.callback(interaction)


if __name__ == "__main__":
    unittest.main()

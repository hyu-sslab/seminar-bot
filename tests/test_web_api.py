from __future__ import annotations

import asyncio
import io
import json
import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from urllib.parse import quote

from aiohttp.test_utils import TestClient, TestServer
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from seminar_bot.access import AccessUnavailable
from seminar_bot.bot import SeminarBot
from seminar_bot.documents import DocumentInfo
from seminar_bot.parser_service import create_parser_app
from seminar_bot.settings import Settings
from seminar_bot.web_api import SeminarAPI, session_version


def pdf_bytes(title: str, body: str = "") -> bytes:
    result = io.BytesIO()
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    if body:
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {
                        NameObject("/F1"): DictionaryObject(
                            {
                                NameObject("/Type"): NameObject("/Font"),
                                NameObject("/Subtype"): NameObject("/Type1"),
                                NameObject("/BaseFont"): NameObject("/Helvetica"),
                            }
                        )
                    }
                )
            }
        )
        text = body.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/Title": title})
    writer.write(result)
    return result.getvalue()


class WebAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_dir = self.root / "data"
        self.today = datetime.now(UTC).date()
        self.day = self.today + timedelta(days=1)
        self.parser = TestServer(create_parser_app())
        await self.parser.start_server()
        self.bot = SeminarBot(
            Settings(
                token="test-discord-token-never-used",
                data_dir=self.data_dir,
                rotation_file=self.root / "rotation.json",
                api_token="x" * 64,
                api_guild_id=1,
                seminar_page_url="https://site.test/internal/seminars/",
                parser_url=str(self.parser.make_url("/")).rstrip("/"),
                access_team_domain="example.cloudflareaccess.com",
                access_audience="a" * 64,
            )
        )
        self.bot.is_ready = Mock(return_value=True)
        self.bot.get_guild = Mock(return_value=SimpleNamespace(get_member=lambda user_id: None))
        self.bot.reload_rotation_file = AsyncMock()
        self.bot.notify_web_submission = AsyncMock(return_value=123)
        self.bot.store.configure(
            1, 1001, 1002, self.day.weekday(), "09:00", "18:00", "UTC", datetime.now(UTC)
        )
        self.bot.store.set_rotation(1, [101, 102], self.day, self.today)
        self.row = self.bot.store.session_on(1, self.day)
        self.version = session_version(self.row)
        self.api = SeminarAPI(self.bot)
        self.api.access.verify = AsyncMock(return_value=True)
        self.client = TestClient(TestServer(self.api.app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.parser.close()
        await self.bot.close()
        self.temp.cleanup()

    def headers(self, **extra):
        return {
            "Authorization": "Bearer " + self.bot.settings.api_token,
            "Cf-Access-Jwt-Assertion": "test-access-assertion",
            "X-Seminar-Version": self.version,
            **extra,
        }

    async def upload(
        self, kind="slides", data=None, filename="presentation.pdf", key=None, headers=None
    ):
        return await self.client.put(
            f"/v1/sessions/{self.row['id']}/documents?kind={kind}",
            data=pdf_bytes("A Seminar Presentation") if data is None else data,
            headers=self.headers(
                **{
                    "Content-Type": "application/octet-stream",
                    "X-Filename": quote(filename, safe=""),
                    "Idempotency-Key": key or str(uuid.uuid4()),
                    **(headers or {}),
                }
            ),
        )

    async def submit(self, headers=None):
        return await self.client.post(
            f"/v1/sessions/{self.row['id']}/submit",
            json={},
            headers=self.headers(**(headers or {})),
        )

    def count_documents(self):
        return self.bot.store.db.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    def files(self):
        return sorted(path for path in (self.data_dir / "files").rglob("*") if path.is_file())

    def assert_private(self, response):
        self.assertEqual(response.headers["Cache-Control"], "private, no-store")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    async def complete_uploads(self):
        slide = await self.upload()
        self.assertEqual(slide.status, 201, await slide.text())
        for name in [
            "Research Paper Topic One",
            "Research Paper Topic Two",
            "Research Paper Topic Three",
        ]:
            response = await self.upload("paper", pdf_bytes(name), name + ".pdf")
            self.assertEqual(response.status, 201, await response.text())

    async def test_bearer_and_access_auth_protect_schedule_upload_and_download(self):
        for path, method in [
            ("/v1/sessions", "get"),
            ("/v1/documents/1/file", "get"),
            (f"/v1/sessions/{self.row['id']}/documents?kind=slides", "put"),
        ]:
            response = await getattr(self.client, method)(path)
            self.assertEqual(response.status, 401)
            self.assert_private(response)
        self.api.access.verify.assert_not_awaited()
        self.api.access.verify.return_value = False
        response = await self.client.get("/v1/sessions", headers=self.headers())
        self.assertEqual(response.status, 401)
        self.api.access.verify.assert_awaited_once_with("test-access-assertion")
        self.api.access.verify.side_effect = AccessUnavailable("private service diagnostic")
        response = await self.client.get("/v1/sessions", headers=self.headers())
        self.assertEqual(response.status, 503)
        self.assertNotIn("private service diagnostic", await response.text())

    async def test_disconnected_bot_fails_closed_but_authenticated_health_reports_readiness(self):
        self.bot.is_ready.return_value = False
        response = await self.client.get("/v1/sessions", headers=self.headers())
        self.assertEqual(response.status, 503)
        health = await self.client.get("/v1/health", headers=self.headers())
        self.assertEqual(health.status, 200)
        self.assertEqual(await health.json(), {"ok": True, "ready": False})

    async def test_private_vpc_requires_both_independent_credentials_without_access_fallback(self):
        self.bot.settings = replace(
            self.bot.settings,
            api_transport="vpc",
            relay_token="r" * 64,
            access_team_domain="",
            access_audience="",
        )
        api = SeminarAPI(self.bot)
        self.assertIsNone(api.access)
        client = TestClient(TestServer(api.app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        bearer = "Bearer " + self.bot.settings.api_token
        for headers in (
            {},
            {"Authorization": bearer},
            {"X-Seminar-Relay-Token": "r" * 64},
            {"Authorization": bearer, "Cf-Access-Jwt-Assertion": "a-public-access-assertion"},
            {"Authorization": bearer, "X-Seminar-Relay-Token": "wrong"},
        ):
            response = await client.get("/v1/health", headers=headers)
            self.assertEqual(response.status, 401)
            self.assert_private(response)
        credentials = {"Authorization": bearer, "X-Seminar-Relay-Token": "r" * 64}
        response = await client.get("/v1/sessions", headers=credentials)
        self.assertEqual(response.status, 200)
        response = await client.put(
            f"/v1/sessions/{self.row['id']}/documents?kind=paper",
            data=pdf_bytes("Private VPC Research Paper"),
            headers={
                **credentials,
                "Content-Type": "application/octet-stream",
                "X-Filename": "private-paper.pdf",
                "Idempotency-Key": str(uuid.uuid4()),
                "X-Seminar-Version": self.version,
            },
        )
        self.assertEqual(response.status, 201, await response.text())
        self.assertFalse((await response.json())["document"]["file_available"])
        self.assertEqual(self.files(), [])

    async def test_private_vpc_api_constructor_cannot_bypass_missing_or_reused_relay_secret(self):
        for secret in ("", self.bot.settings.api_token):
            self.bot.settings = replace(self.bot.settings, api_transport="vpc", relay_token=secret)
            with self.assertRaises(ValueError):
                SeminarAPI(self.bot)

    async def test_session_list_and_detail_expose_names_version_and_scoped_progress(self):
        self.bot.settings.rotation_file.write_text(
            json.dumps(
                {
                    "guilds": {
                        "1": {
                            "presenters": [
                                {"user_id": "101", "name": "발표자 A"},
                                {"user_id": "102", "name": "발표자 B"},
                            ]
                        }
                    }
                }
            )
        )
        response = await self.client.get("/v1/sessions", headers=self.headers())
        self.assertEqual(response.status, 200)
        self.assert_private(response)
        payload = await response.json()
        session = next(row for row in payload["sessions"] if row["id"] == self.row["id"])
        self.assertEqual(session["presenter_name"], "발표자 A")
        self.assertEqual(session["presenter_id"], "101")
        self.assertEqual(session["version"], self.version)
        detail = await self.client.get(f"/v1/sessions/{self.row['id']}", headers=self.headers())
        self.assertEqual((await detail.json())["documents"], [])
        self.assertEqual(
            (
                await self.client.get("/v1/sessions/9223372036854775808", headers=self.headers())
            ).status,
            404,
        )

    async def test_pdf_paper_roundtrip_retains_only_searchable_metadata(self):
        before_temp = set((Path(tempfile.gettempdir()) / "seminar-paper-uploads").glob("web-*"))
        body = "Sensitive full paper body must never be retained."
        extracted = []
        actual_extract = self.bot.extract_document

        async def capture_extraction(*args):
            info = await actual_extract(*args)
            extracted.append(info.text)
            return info

        self.bot.extract_document = capture_extraction
        response = await self.upload(
            "paper", pdf_bytes("Metadata Only Research Paper", body), "참고 논문.pdf"
        )
        self.assertEqual(response.status, 201)
        self.assert_private(response)
        payload = await response.json()
        document = payload["document"]
        self.assertEqual(document["title"], "Metadata Only Research Paper")
        self.assertFalse(document["file_available"])
        self.assertEqual(document["source"], "web")
        stored = self.bot.store.document(1, document["id"])
        self.assertEqual(stored["file_path"], "")
        self.assertEqual(stored["extracted_text"], "")
        self.assertEqual(stored["extraction_method"], "pdf_metadata")
        self.assertEqual(extracted, [body])
        self.assertEqual(self.files(), [])
        self.assertEqual(
            before_temp, set((Path(tempfile.gettempdir()) / "seminar-paper-uploads").glob("web-*"))
        )
        download = await self.client.get(
            f"/v1/documents/{document['id']}/file", headers=self.headers()
        )
        self.assertEqual(download.status, 404)

    async def test_slides_pdf_is_preserved_and_downloaded_as_private_attachment(self):
        data = pdf_bytes("A Preserved Presentation")
        response = await self.upload(data=data, filename="발표 자료.pdf")
        self.assertEqual(response.status, 201)
        payload = await response.json()
        document = payload["document"]
        self.assertTrue(document["file_available"])
        download = await self.client.get(
            f"/v1/documents/{document['id']}/file", headers=self.headers()
        )
        self.assertEqual(download.status, 200)
        self.assert_private(download)
        self.assertTrue(download.headers["Content-Disposition"].startswith("attachment;"))
        self.assertIn(quote("발표 자료.pdf", safe=""), download.headers["Content-Disposition"])
        self.assertEqual(await download.read(), data)
        self.assertEqual(len(self.files()), 1)

    async def test_search_and_detail_never_expose_paths_body_tokens_or_private_document_fields(
        self,
    ):
        response = await self.upload(
            "paper", pdf_bytes("Searchable Metadata Research"), "reference.pdf"
        )
        self.assertEqual(response.status, 201)
        for path in ["/v1/search?q=Metadata", f"/v1/sessions/{self.row['id']}"]:
            response = await self.client.get(path, headers=self.headers())
            self.assertEqual(response.status, 200)
            body = await response.text()
            self.assertIn("Searchable Metadata Research", body)
            for forbidden in [
                "file_path",
                "extracted_text",
                "upload_key",
                self.bot.settings.api_token,
                self.bot.settings.token,
                str(self.data_dir),
                "test-access-assertion",
            ]:
                self.assertNotIn(forbidden, body)

    async def test_upload_rejects_bad_filenames_categories_uuids_empty_and_non_document_bytes(self):
        for headers in [
            {"X-Filename": "..%2Fprivate.pdf"},
            {"X-Filename": "bad%0Aname.pdf"},
            {"X-Filename": "%FF.pdf"},
            {"X-Filename": "%invalid"},
            {"X-Filename": quote("한" * 80 + ".pdf", safe="")},
            {"Idempotency-Key": "invalid"},
        ]:
            self.assertEqual((await self.upload(headers=headers)).status, 400)
        for kwargs in [
            {"kind": "unknown"},
            {"kind": "paper", "filename": "reference.ppt"},
            {"filename": "file.html"},
            {"data": b""},
            {"data": b"not a PDF"},
        ]:
            self.assertEqual((await self.upload(**kwargs)).status, 400)
        self.assertEqual((await self.upload(headers={"Content-Type": "text/plain"})).status, 415)
        self.assertEqual(self.count_documents(), 0)
        self.assertEqual(self.files(), [])

    async def test_declared_and_chunked_upload_overflow_leave_no_records_or_files(self):
        self.api.max_bytes = 1024
        self.assertEqual((await self.upload(data=b"%PDF-" + b"x" * 1024)).status, 413)

        async def chunks():
            yield b"%PDF-" + b"x" * 600
            yield b"x" * 600

        self.assertEqual((await self.upload(data=chunks())).status, 413)
        self.assertEqual(self.count_documents(), 0)
        self.assertEqual(self.files(), [])

    async def test_unavailable_isolated_parser_fails_closed_and_cleans_uploaded_file(self):
        await self.parser.close()
        with self.assertLogs("seminar_bot.web_api", level="ERROR"):
            response = await self.upload()
        self.assertEqual(response.status, 503)
        self.assertNotIn("Traceback", await response.text())
        self.assertEqual(self.count_documents(), 0)
        self.assertEqual(self.files(), [])

    async def test_canonical_uuid_retry_returns_existing_record_without_extra_files(self):
        key = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        first = await self.upload(key=key.upper())
        self.assertEqual(first.status, 201)
        first_payload = await first.json()
        second = await self.upload(key=key)
        self.assertEqual(second.status, 200)
        payload = await second.json()
        self.assertTrue(payload["duplicate"])
        self.assertEqual(payload["document"]["id"], first_payload["document"]["id"])
        self.assertEqual(self.count_documents(), 1)
        self.assertEqual(len(self.files()), 1)

    async def test_reused_upload_key_conflicts_with_changed_bytes_name_or_category(self):
        key = str(uuid.uuid4())
        self.assertEqual((await self.upload(key=key)).status, 201)
        for kwargs in [
            {"data": pdf_bytes("Different Presentation")},
            {"filename": "renamed.pdf"},
            {"kind": "paper"},
        ]:
            self.assertEqual((await self.upload(key=key, **kwargs)).status, 409)
        self.assertEqual(self.count_documents(), 1)
        self.assertEqual(len(self.files()), 1)

    async def test_simultaneous_retry_of_same_upload_is_atomic(self):
        key = str(uuid.uuid4())
        responses = await asyncio.gather(self.upload(key=key), self.upload(key=key))
        self.assertEqual(sorted(response.status for response in responses), [200, 201])
        payloads = [await response.json() for response in responses]
        self.assertEqual(len({payload["document"]["id"] for payload in payloads}), 1)
        self.assertEqual(self.count_documents(), 1)
        self.assertEqual(len(self.files()), 1)

    async def test_idempotent_replay_cannot_reveal_documents_from_previous_upload_channel(self):
        key = str(uuid.uuid4())
        data = pdf_bytes("Hidden Previous Channel Paper")
        response = await self.upload("paper", data, "private.pdf", key=key)
        self.assertEqual(response.status, 201)
        self.bot.store.db.execute("UPDATE guilds SET upload_channel_id=9999 WHERE guild_id=1")
        self.bot.store.db.commit()
        response = await self.upload("paper", data, "private.pdf", key=key)
        self.assertEqual(response.status, 409)
        self.assertNotIn("Hidden Previous Channel Paper", await response.text())
        self.assertEqual(self.count_documents(), 1)

    async def test_complete_submission_notifies_once_and_repeated_submit_is_idempotent(self):
        await self.complete_uploads()
        for _ in range(2):
            response = await self.submit()
            self.assertEqual(response.status, 200)
            payload = await response.json()
            self.assertTrue(payload["submission"]["complete"])
            self.assertTrue(payload["notification_sent"])
            self.assertEqual(payload["submission"]["paper_count"], 3)
        self.bot.notify_web_submission.assert_awaited_once()
        self.assertEqual(self.count_documents(), 4)

    async def test_duplicate_paper_bytes_count_once_and_incomplete_submit_preserves_uploads(self):
        self.assertEqual((await self.upload()).status, 201)
        paper = pdf_bytes("Same Research Paper")
        for index in range(3):
            self.assertEqual((await self.upload("paper", paper, f"copy-{index}.pdf")).status, 201)
        response = await self.submit()
        self.assertEqual(response.status, 409)
        payload = await response.json()
        self.assertEqual(payload["submission"]["paper_count"], 1)
        self.assertFalse(payload["submission"]["complete"])
        self.assertEqual(self.count_documents(), 4)
        self.bot.notify_web_submission.assert_not_awaited()

    async def test_notification_failure_is_visible_and_retry_delivers_once_without_duplicate_uploads(
        self,
    ):
        await self.complete_uploads()
        self.bot.notify_web_submission.side_effect = [
            RuntimeError("private notification diagnostic"),
            123,
        ]
        with self.assertLogs("seminar_bot.web_api", level="ERROR"):
            first = await self.submit()
        self.assertEqual(first.status, 200)
        payload = await first.json()
        self.assertFalse(payload["notification_sent"])
        self.assertIn("notification_warning", payload)
        self.assertNotIn("private notification diagnostic", json.dumps(payload))
        for _ in range(2):
            retry = await self.submit()
            self.assertTrue((await retry.json())["notification_sent"])
        self.assertEqual(self.bot.notify_web_submission.await_count, 2)
        self.assertEqual(self.count_documents(), 4)

    async def test_stale_session_versions_block_upload_and_submit_before_any_file_is_saved(self):
        self.bot.store.postpone(1, self.today, self.day)
        for response in [await self.upload(), await self.submit()]:
            self.assertEqual(response.status, 409)
            self.assertEqual((await response.json())["code"], "session_changed")
        self.assertEqual(self.count_documents(), 0)
        self.assertEqual(self.files(), [])

    async def test_schedule_change_during_parsing_rejects_commit_and_cleans_file(self):
        actual_extract = self.bot.extract_document

        async def change_after_parsing(*args):
            result = await actual_extract(*args)
            self.bot.store.postpone(1, self.today, self.day)
            return result

        self.bot.extract_document = change_after_parsing
        response = await self.upload()
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["code"], "session_changed")
        self.assertEqual(self.count_documents(), 0)
        self.assertEqual(self.files(), [])

    async def test_schedule_change_during_idempotent_replay_still_rejects_stale_version(self):
        key = str(uuid.uuid4())
        data = pdf_bytes("Replay Research Paper")
        self.assertEqual((await self.upload("paper", data, "replay.pdf", key=key)).status, 201)
        snapshot_checked = asyncio.Event()
        actual_session = self.api._session

        def capture_snapshot(request):
            row = actual_session(request)
            snapshot_checked.set()
            return row

        self.api._session = capture_snapshot

        async def slow_body():
            yield data[:32]
            await snapshot_checked.wait()
            self.bot.store.postpone(1, self.today, self.day)
            yield data[32:]

        response = await self.upload("paper", slow_body(), "replay.pdf", key=key)
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())["code"], "session_changed")
        self.assertEqual(self.count_documents(), 1)
        self.assertEqual(self.files(), [])

    async def test_other_guild_and_hidden_channel_documents_are_not_searchable_or_downloadable(
        self,
    ):
        self.bot.store.configure(
            2, 2001, 2002, self.day.weekday(), "09:00", "18:00", "UTC", datetime.now(UTC)
        )
        self.bot.store.set_rotation(2, [201], self.day, self.today)
        info = DocumentInfo(
            "Hidden Research Paper", "private extracted body", None, None, "f" * 64, "pdf_metadata"
        )
        ids = []
        for guild_id, uploader, channel in [(2, 201, 2002), (1, 101, 9999)]:
            record_id, _ = self.bot.store.add_document(
                guild_id,
                9000 + guild_id,
                uploader,
                info,
                "hidden.pdf",
                "files/hidden.pdf",
                "https://discord.com/channels/1/2/3",
                datetime.now(UTC),
                source_channel_id=channel,
                kind="paper",
            )
            ids.append(record_id)
        response = await self.client.get("/v1/search?q=Hidden&kind=all", headers=self.headers())
        self.assertEqual((await response.json())["documents"], [])
        for record_id in ids:
            response = await self.client.get(
                f"/v1/documents/{record_id}/file", headers=self.headers()
            )
            self.assertEqual(response.status, 404)
        foreign = self.bot.store.session_on(2, self.day)
        self.assertEqual(
            (await self.client.get(f"/v1/sessions/{foreign['id']}", headers=self.headers())).status,
            404,
        )
        detail = await self.client.get(f"/v1/sessions/{self.row['id']}", headers=self.headers())
        self.assertEqual((await detail.json())["documents"], [])

    async def test_download_rejects_path_traversal_and_symlink_escape(self):
        response = await self.upload()
        record_id = (await response.json())["document"]["id"]
        outside = self.root / "outside.pdf"
        outside.write_bytes(b"private outside document")
        symlink = self.data_dir / "files" / "escape.pdf"
        symlink.symlink_to(outside)
        for path in ["../outside.pdf", "files/escape.pdf", str(outside)]:
            self.bot.store.db.execute(
                "UPDATE documents SET file_path=? WHERE id=?", (path, record_id)
            )
            self.bot.store.db.commit()
            response = await self.client.get(
                f"/v1/documents/{record_id}/file", headers=self.headers()
            )
            self.assertEqual(response.status, 404)
            self.assertNotIn("private outside document", await response.text())

    async def test_web_source_url_tracks_the_current_session_after_presenter_swap(self):
        response = await self.upload(
            "paper", pdf_bytes("Paper Before Presenter Swap"), "reference.pdf"
        )
        record_id = (await response.json())["document"]["id"]
        other = self.bot.store.session_on(1, self.day + timedelta(days=7))
        self.bot.store.swap(1, self.day, self.day + timedelta(days=7), self.today)
        record = self.bot.store.document(1, record_id)
        self.assertEqual(record["session_id"], other["id"])
        self.assertEqual(self.bot.source_url(record), self.bot.settings.submission_url(other["id"]))

from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote

from aiohttp.test_utils import TestClient, TestServer
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from seminar_bot.access import AccessUnavailable
from seminar_bot.bot import SeminarBot
from seminar_bot.documents import DocumentInfo
from seminar_bot.parser_service import create_parser_app
from seminar_bot.remote_parser import ParserUnavailable
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

    async def test_three_presenters_share_four_upload_slots_and_queries_remain_available(self):
        self.bot.store.set_rotation(1, [101, 102, 103], self.day, self.today)
        sessions = self.bot.store.upcoming(1, self.today, 3)
        gate = asyncio.Event()
        four_started = asyncio.Event()
        active = maximum = started = 0
        kinds = []

        async def extract(path, filename, data, *, kind="paper"):
            nonlocal active, maximum, started
            active += 1
            started += 1
            maximum = max(maximum, active)
            kinds.append(kind)
            if started == 4:
                four_started.set()
            try:
                await gate.wait()
                return DocumentInfo(
                    filename.removesuffix(".pdf"),
                    "",
                    None,
                    None,
                    hashlib.sha256(data).hexdigest(),
                    "filename",
                )
            finally:
                active -= 1

        self.bot.extract_document = extract
        requests = []
        for presenter, session in enumerate(sessions):
            for index in range(4):
                kind = "slides" if index == 0 else "paper"
                filename = f"Presenter-{presenter}-Research-Study-{index}.pdf"
                requests.append(
                    asyncio.create_task(
                        self.client.put(
                            f"/v1/sessions/{session['id']}/documents?kind={kind}",
                            data=pdf_bytes(filename),
                            headers=self.headers(
                                **{
                                    "Content-Type": "application/octet-stream",
                                    "X-Filename": filename,
                                    "Idempotency-Key": str(uuid.uuid4()),
                                    "X-Seminar-Version": session_version(session),
                                }
                            ),
                        )
                    )
                )
        try:
            await asyncio.wait_for(four_started.wait(), 2)
            await asyncio.sleep(0.02)
            self.assertEqual(started, 4)
            for endpoint in ("/v1/health", "/v1/archive?kind=paper"):
                response = await asyncio.wait_for(
                    self.client.get(endpoint, headers=self.headers()), 1
                )
                self.assertEqual(response.status, 200, await response.text())
        finally:
            gate.set()
        responses = await asyncio.gather(*requests)
        self.assertEqual([response.status for response in responses], [201] * 12)
        self.assertEqual(started, 12)
        self.assertEqual(maximum, 4)
        self.assertEqual(active, 0)
        self.assertEqual(kinds.count("slides"), 3)
        self.assertEqual(kinds.count("paper"), 9)
        for session in sessions:
            status = self.api.status(session["id"])
            self.assertEqual(status["slides"], 1)
            self.assertEqual(status["papers"], 3)
            self.assertTrue(status["complete"])

    def configure_notifications(self, enabled):
        config = self.bot.store.require_config(1)
        self.bot.store.configure(
            1,
            config["channel_id"],
            config["upload_channel_id"],
            config["weekday"],
            config["morning"],
            config["evening"],
            config["timezone"],
            notifications_enabled=enabled,
            reminder_start_date=date.fromisoformat(self.row["seminar_date"]) if enabled else None,
        )

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

    async def test_rotation_display_names_use_fixed_api_guild_despite_stale_configured_servers(
        self,
    ):
        self.bot.settings.rotation_file.write_text(
            json.dumps(
                {
                    "start_date": None,
                    "presenters": [{"user_id": "101", "name": "Synthetic Display"}],
                }
            )
        )
        self.assertEqual(await self.api.names(), {101: "Synthetic Display"})
        self.assertEqual(self.api.guild_id, 1)
        self.bot.settings = replace(self.bot.settings, api_guild_id=2)
        other_api = SeminarAPI(self.bot)
        self.assertEqual(await other_api.names(), {101: "Synthetic Display"})
        self.assertEqual(other_api.guild_id, 2)
        for guild_id in (2, 3):
            self.bot.store.configure(
                guild_id,
                2001,
                2002,
                self.day.weekday(),
                "09:00",
                "18:00",
                "UTC",
                datetime.now(UTC),
            )
        before = "\n".join(self.bot.store.db.iterdump())
        self.assertEqual(await self.api.names(), {101: "Synthetic Display"})
        self.assertEqual(await other_api.names(), {101: "Synthetic Display"})
        self.assertEqual(self.api.guild_id, 1)
        self.assertEqual(other_api.guild_id, 2)
        self.bot.settings.rotation_file.write_text(
            json.dumps(
                {"guilds": {"1": {"presenters": [{"user_id": "101", "name": "Legacy Display"}]}}}
            )
        )
        self.assertEqual(await self.api.names(), {101: "Legacy Display"})
        self.assertEqual(await other_api.names(), {})
        self.assertEqual("\n".join(self.bot.store.db.iterdump()), before)

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
        for transport in ("vpc", "relay"):
            for secret in ("", self.bot.settings.api_token):
                self.bot.settings = replace(
                    self.bot.settings, api_transport=transport, relay_token=secret
                )
                with self.assertRaises(ValueError):
                    SeminarAPI(self.bot)

    async def test_outbound_relay_api_still_requires_both_local_credentials(self):
        self.bot.settings = replace(self.bot.settings, api_transport="relay", relay_token="r" * 64)
        api = SeminarAPI(self.bot)
        self.assertIsNone(api.access)
        client = TestClient(TestServer(api.app))
        await client.start_server()
        self.addAsyncCleanup(client.close)
        bearer = "Bearer " + self.bot.settings.api_token
        for credentials in (
            {"Authorization": bearer},
            {"X-Seminar-Relay-Token": "r" * 64},
            {"Authorization": bearer, "X-Seminar-Relay-Token": "wrong"},
        ):
            response = await client.get("/v1/health", headers=credentials)
            self.assertEqual(response.status, 401)
        response = await client.get(
            "/v1/health",
            headers={"Authorization": bearer, "X-Seminar-Relay-Token": "r" * 64},
        )
        self.assertEqual(response.status, 200)

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

        async def capture_extraction(*args, **kwargs):
            info = await actual_extract(*args, **kwargs)
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

    async def test_verified_paper_title_is_stored_without_pdf_or_extracted_body(self):
        title = "Complete Verified Research Paper Title"

        async def verified(info):
            self.assertEqual(info.doi, "10.1234/verified")
            self.assertIn(title, info.text)
            return replace(info, title=title)

        with patch.object(self.bot.paper_metadata, "enrich", AsyncMock(side_effect=verified)):
            response = await self.upload(
                "paper", pdf_bytes("Truncated Research Title", title + " DOI:10.1234/verified")
            )
        self.assertEqual(response.status, 201, await response.text())
        document = (await response.json())["document"]
        self.assertEqual(document["title"], title)
        stored = self.bot.store.document(1, document["id"])
        self.assertEqual(stored["doi"], "10.1234/verified")
        self.assertEqual(stored["extracted_text"], "")
        self.assertEqual(stored["file_path"], "")
        self.assertEqual(self.files(), [])

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

    async def test_error_diagnostics_correlate_request_log_without_private_exception_text(self):
        request_id = str(uuid.uuid4())
        private = "private-exception-with-internal-path-and-credentials"
        with (
            patch.object(
                self.bot, "extract_document", AsyncMock(side_effect=RuntimeError(private))
            ),
            self.assertLogs("seminar_bot.web_api", level="ERROR") as logs,
        ):
            response = await self.upload(headers={"X-Seminar-Request-ID": request_id})
        payload = await response.json()
        self.assertEqual(response.status, 503)
        self.assertEqual(payload["code"], "internal_error")
        self.assertEqual(payload["diagnostics"]["request_id"], request_id)
        self.assertEqual(payload["diagnostics"]["stage"], "nas_parse")
        self.assertGreater(payload["diagnostics"]["uploaded_bytes"], 0)
        self.assertGreaterEqual(payload["diagnostics"]["elapsed_ms"], 0)
        self.assertEqual(response.headers["X-Seminar-Request-ID"], request_id)
        self.assertIn(request_id, "".join(logs.output))
        self.assertIn('"exception_type": "RuntimeError"', "".join(logs.output))
        self.assertNotIn("exception_type", payload["diagnostics"])
        self.assertNotIn(private, json.dumps(payload) + "".join(logs.output))
        self.assertNotIn(self.bot.settings.api_token, json.dumps(payload) + "".join(logs.output))
        self.assertEqual(self.count_documents(), 0)
        self.assertEqual(self.files(), [])

    async def test_invalid_request_id_is_replaced_even_on_auth_failure(self):
        response = await self.client.get(
            "/v1/sessions", headers={"X-Seminar-Request-ID": "not-a-uuid-private-value"}
        )
        payload = await response.json()
        self.assertEqual(response.status, 401)
        self.assertEqual(payload["code"], "nas_auth_failed")
        self.assertEqual(payload["diagnostics"]["stage"], "nas_auth")
        self.assertEqual(
            str(uuid.UUID(payload["diagnostics"]["request_id"])),
            response.headers["X-Seminar-Request-ID"],
        )
        self.assertNotIn("not-a-uuid-private-value", await response.text())

    async def test_parser_diagnostics_distinguish_timeout_rejection_and_invalid_response(self):
        for code, upstream in (
            ("parser_timeout", None),
            ("parser_rejected", 503),
            ("parser_invalid_response", None),
            ("parser_config_invalid", None),
            ("not-a-known-code", None),
        ):
            with (
                self.subTest(code=code),
                patch.object(
                    self.bot,
                    "extract_document",
                    AsyncMock(side_effect=ParserUnavailable("private exception", code, upstream)),
                ),
                self.assertLogs("seminar_bot.web_api", level="ERROR"),
            ):
                response = await self.upload()
            payload = await response.json()
            self.assertEqual(response.status, 503)
            self.assertEqual(
                payload["code"], code if code != "not-a-known-code" else "parser_unavailable"
            )
            self.assertEqual(payload["diagnostics"]["stage"], "nas_parse")
            self.assertEqual(payload["diagnostics"].get("upstream_status"), upstream)
            self.assertNotIn("private exception", await response.text())
            self.assertEqual(self.files(), [])

    async def test_api_timeout_and_connection_loss_have_distinct_safe_diagnostics(self):
        for failure, status, code in (
            (TimeoutError("private timeout detail"), 408, "upload_timeout"),
            (ConnectionError("private connection detail"), 503, "connection_interrupted"),
        ):
            with (
                self.subTest(code=code),
                patch.object(self.bot, "extract_document", AsyncMock(side_effect=failure)),
                self.assertLogs("seminar_bot.web_api", level="WARNING"),
            ):
                response = await self.upload()
            payload = await response.json()
            self.assertEqual(response.status, status)
            self.assertEqual(payload["code"], code)
            self.assertEqual(payload["diagnostics"]["stage"], "nas_parse")
            self.assertNotIn("private", await response.text())
            self.assertEqual(self.files(), [])

    async def test_storage_diagnostics_distinguish_permissions_capacity_and_io_failure(self):
        for failure, code in (
            (PermissionError("private NAS path"), "storage_permission_denied"),
            (OSError(errno.ENOSPC, "private NAS path"), "storage_full"),
            (OSError(errno.EDQUOT, "private NAS path"), "storage_full"),
            (OSError(errno.EIO, "private NAS path"), "storage_unavailable"),
        ):
            with (
                self.subTest(code=code),
                patch("seminar_bot.web_api.shutil.disk_usage", side_effect=failure),
                self.assertLogs("seminar_bot.web_api", level="ERROR"),
            ):
                response = await self.upload()
            payload = await response.json()
            self.assertEqual(response.status, 503)
            self.assertEqual(payload["code"], code)
            self.assertEqual(payload["diagnostics"]["stage"], "nas_storage")
            self.assertNotIn("private NAS path", await response.text())

    async def test_database_diagnostics_distinguish_locked_database(self):
        for error_code, code in (
            (sqlite3.SQLITE_BUSY, "database_busy"),
            (sqlite3.SQLITE_ERROR, "database_error"),
        ):
            failure = sqlite3.OperationalError("private database detail")
            failure.sqlite_errorcode = error_code
            with (
                self.subTest(code=code),
                patch.object(self.bot.store, "add_document", side_effect=failure),
                self.assertLogs("seminar_bot.web_api", level="ERROR"),
            ):
                response = await self.upload()
            payload = await response.json()
            self.assertEqual(response.status, 503)
            self.assertEqual(payload["code"], code)
            self.assertEqual(payload["diagnostics"]["stage"], "nas_store")
            self.assertNotIn("private database detail", await response.text())
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

    async def test_notification_channel_change_keeps_web_archive_scope_grant_and_replay(self):
        invitation, _ = self.api.submission_access.issue(1, self.row["id"], 101, scope="presenter")
        grant, _ = self.api.submission_access.redeem(invitation, 1)
        scoped = {"X-Seminar-Submission-Token": grant}
        key = str(uuid.uuid4())
        data = pdf_bytes("Existing Seminar Presentation")
        uploaded = await self.upload(data=data, key=key, headers=scoped)
        self.assertEqual(uploaded.status, 201)
        document = (await uploaded.json())["document"]
        hidden_path = self.data_dir / "files" / "other-channel.pdf"
        hidden_path.write_bytes(pdf_bytes("Hidden Channel Presentation"))
        hidden_id, _ = self.bot.store.add_document(
            1,
            999,
            101,
            DocumentInfo(
                "Hidden Channel Presentation",
                "private body",
                None,
                None,
                "hidden-sha",
                "pdf_metadata",
            ),
            "other-channel.pdf",
            "files/other-channel.pdf",
            "https://site.test/internal/seminars/",
            datetime.now(UTC),
            source_channel_id=999,
            kind="slides",
            session_id=self.row["id"],
            source_type="web",
            upload_key=str(uuid.uuid4()),
        )
        target = SimpleNamespace(
            id=2001,
            guild=SimpleNamespace(id=1),
            mention="<#2001>",
            permissions_for=lambda _: SimpleNamespace(
                view_channel=True, send_messages=True, embed_links=True, read_message_history=True
            ),
        )
        interaction = SimpleNamespace(
            guild_id=1,
            guild=SimpleNamespace(me=object()),
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        await self.bot.tree.get_command("설정").callback(
            interaction,
            self.day.weekday(),
            target,
            "14:00",
            timezone="UTC",
        )
        config = self.bot.store.require_config(1)
        self.assertEqual((config["channel_id"], config["upload_channel_id"]), (2001, 1002))
        context = await self.client.get("/v1/submission/context", headers=self.headers(**scoped))
        self.assertEqual(context.status, 200)
        self.assertEqual((await context.json())["session_id"], self.row["id"])
        detail = await self.client.get(
            f"/v1/sessions/{self.row['id']}", headers=self.headers(**scoped)
        )
        self.assertEqual(detail.status, 200)
        self.assertEqual(
            [row["id"] for row in (await detail.json())["documents"]], [document["id"]]
        )
        archive = await self.client.get("/v1/archive", headers=self.headers())
        self.assertEqual(archive.status, 200)
        self.assertEqual(
            [row["id"] for row in (await archive.json())["documents"]], [document["id"]]
        )
        search = await self.client.get("/v1/search?q=Presentation&kind=all", headers=self.headers())
        self.assertEqual(
            [row["id"] for row in (await search.json())["documents"]], [document["id"]]
        )
        download = await self.client.get(
            f"/v1/documents/{document['id']}/file", headers=self.headers()
        )
        self.assertEqual(download.status, 200)
        self.assertEqual(await download.read(), data)
        self.assertEqual(
            (
                await self.client.get(f"/v1/documents/{hidden_id}/file", headers=self.headers())
            ).status,
            404,
        )
        for method in ("patch", "delete"):
            hidden = await getattr(self.client, method)(
                f"/v1/sessions/{self.row['id']}/documents/{hidden_id}",
                json={"title": "Not allowed"} if method == "patch" else {},
                headers=self.headers(**scoped),
            )
            self.assertEqual(hidden.status, 404)
        replay = await self.upload(data=data, key=key, headers=scoped)
        self.assertEqual(replay.status, 200)
        self.assertTrue((await replay.json())["duplicate"])
        self.assertEqual(self.count_documents(), 2)
        self.assertEqual(self.bot.store.document(1, document["id"])["source_channel_id"], 1002)
        self.assertTrue(hidden_path.exists())

    async def test_complete_submission_notifies_once_and_repeated_submit_is_idempotent(self):
        self.configure_notifications(True)
        await self.complete_uploads()
        for _ in range(2):
            response = await self.submit()
            self.assertEqual(response.status, 200)
            payload = await response.json()
            self.assertTrue(payload["submission"]["complete"])
            self.assertTrue(payload["notification_sent"])
            self.assertFalse(payload["notifications_disabled"])
            self.assertEqual(payload["submission"]["paper_count"], 3)
        self.bot.notify_web_submission.assert_awaited_once()
        self.assertEqual(self.count_documents(), 4)

    async def test_disabled_notifications_complete_submission_without_messages_or_receipts(self):
        self.assertFalse(self.bot.store.require_config(1)["notifications_enabled"])
        await self.complete_uploads()
        for _ in range(2):
            response = await self.submit()
            self.assertEqual(response.status, 200)
            payload = await response.json()
            self.assertTrue(payload["submission"]["complete"])
            self.assertFalse(payload["notification_sent"])
            self.assertTrue(payload["notifications_disabled"])
            self.assertNotIn("notification_warning", payload)
        self.bot.notify_web_submission.assert_not_awaited()
        self.assertIsNone(self.bot.store.submission_receipt(self.row["id"]))
        self.assertEqual(self.count_documents(), 4)

    async def test_submission_while_disabled_can_notify_once_after_reenabling(self):
        await self.complete_uploads()
        disabled = await self.submit()
        self.assertEqual(disabled.status, 200)
        self.assertTrue((await disabled.json())["notifications_disabled"])
        self.assertIsNone(self.bot.store.submission_receipt(self.row["id"]))
        self.configure_notifications(True)
        for _ in range(2):
            response = await self.submit()
            self.assertEqual(response.status, 200)
            payload = await response.json()
            self.assertTrue(payload["notification_sent"])
            self.assertFalse(payload["notifications_disabled"])
            self.assertNotIn("notification_warning", payload)
        self.bot.notify_web_submission.assert_awaited_once()
        self.assertIsNotNone(self.bot.store.submission_receipt(self.row["id"]))

    async def test_disabling_notifications_preserves_existing_receipt_without_new_message(self):
        self.configure_notifications(True)
        await self.complete_uploads()
        self.assertEqual((await self.submit()).status, 200)
        receipt = self.bot.store.submission_receipt(self.row["id"])
        self.assertIsNotNone(receipt)
        self.configure_notifications(False)
        response = await self.submit()
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertFalse(payload["notification_sent"])
        self.assertTrue(payload["notifications_disabled"])
        self.assertNotIn("notification_warning", payload)
        self.assertEqual(self.bot.store.submission_receipt(self.row["id"]), receipt)
        self.configure_notifications(True)
        self.assertTrue((await (await self.submit()).json())["notification_sent"])
        self.bot.notify_web_submission.assert_awaited_once()

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
        self.configure_notifications(True)
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

        async def change_after_parsing(*args, **kwargs):
            result = await actual_extract(*args, **kwargs)
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

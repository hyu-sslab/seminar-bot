from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import quote

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from pypdf import PdfWriter

from seminar_bot import parser_service, remote_parser
from seminar_bot.documents import DocumentInfo
from seminar_bot.parser_service import create_parser_app
from seminar_bot.remote_parser import ParserUnavailable, extract_remote


def _pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata({"/Title": "Isolated PDF Roundtrip"})
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _document(data: bytes) -> DocumentInfo:
    return DocumentInfo(
        "Example Document",
        "Example extracted text",
        None,
        None,
        hashlib.sha256(data).hexdigest(),
        "pdf_text",
        None,
    )


class ParserServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = TestClient(TestServer(create_parser_app()))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def _post(self, data=b"%PDF-fixture", filename="paper.pdf", **kwargs):
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Filename": quote(filename, safe=""),
        }
        headers.update(kwargs.pop("headers", {}))
        return await self.client.post("/extract", data=data, headers=headers, **kwargs)

    async def test_real_pdf_roundtrips_through_remote_client_and_isolated_child(self):
        data = _pdf()
        with tempfile.TemporaryDirectory() as directory:
            fake_local_path = Path(directory) / "must-not-be-opened.pdf"
            result = await extract_remote(
                fake_local_path, "연구실 발표.pdf", data, str(self.client.make_url("/"))
            )
        self.assertEqual(result.title, "Isolated PDF Roundtrip")
        self.assertEqual(result.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(result.extraction_method, "pdf_metadata")

    async def test_corrupt_document_is_archived_as_filename_without_local_parser_fallback(self):
        response = await self._post(b"not-a-valid-pdf", "Corrupt Document.pdf")
        self.assertEqual(response.status, 200)
        result = await response.json()
        self.assertEqual(result["title"], "Corrupt Document")
        self.assertEqual(result["extraction_method"], "filename")
        self.assertTrue(result["warning"])
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_filename_and_content_type_validation(self):
        for raw in [
            "",
            "%invalid",
            "%FF.pdf",
            "..%2Fprivate.pdf",
            "name%5Cprivate.pdf",
            "bad%0Aname.pdf",
            "script.html",
        ]:
            with self.subTest(raw=raw):
                response = await self._post(headers={"X-Filename": raw})
                self.assertEqual(response.status, 400)
        self.assertEqual((await self._post(headers={"Content-Type": "text/plain"})).status, 415)
        self.assertEqual((await self._post(b"")).status, 400)
        self.assertEqual((await self._post(filename="한" * 80 + ".pdf")).status, 400)
        self.assertEqual((await self._post(headers={"X-Document-Kind": "unknown"})).status, 400)

    async def test_declared_and_actual_stream_sizes_are_bounded(self):
        async def chunks():
            yield b"12345"
            yield b"67890"

        with (
            patch.object(parser_service, "MAX_PARSER_FILE_BYTES", 8),
            patch.object(
                parser_service, "_extract_saved_isolated", new_callable=AsyncMock
            ) as extraction,
        ):
            self.assertEqual((await self._post(b"123456789")).status, 413)
            self.assertEqual((await self._post(chunks())).status, 413)
            extraction.assert_not_awaited()

    async def test_service_failures_do_not_expose_secrets_paths_or_exception_text(self):
        private = "simulated-private-value-do-not-publish"
        with patch.object(
            parser_service, "_extract_saved_isolated", AsyncMock(side_effect=RuntimeError(private))
        ):
            response = await self._post()
        self.assertEqual(response.status, 503)
        self.assertNotIn(private, await response.text())
        self.assertNotIn("/tmp", await response.text())

    async def test_child_uses_isolated_python_and_minimal_environment(self):
        actual_spawn = asyncio.create_subprocess_exec
        observed = {}

        async def spawn(*args, **kwargs):
            observed.update(args=args, env=kwargs["env"])
            return await actual_spawn(*args, **kwargs)

        with (
            patch.dict(
                os.environ,
                {
                    "DISCORD_TOKEN": "test-private-discord-value",
                    "WEB_API_TOKEN": "test-private-api-value",
                },
            ),
            patch.object(parser_service.asyncio, "create_subprocess_exec", spawn),
        ):
            response = await self._post(_pdf(), headers={"X-Document-Kind": "slides"})
        self.assertEqual(response.status, 200)
        self.assertIn("-I", observed["args"])
        self.assertEqual(observed["args"][-1], "slides")
        self.assertNotIn("DISCORD_TOKEN", observed["env"])
        self.assertNotIn("WEB_API_TOKEN", observed["env"])
        self.assertEqual(set(observed["env"]), {"PATH", "LANG", "LC_ALL", "TMPDIR"})
        body = await response.text()
        self.assertNotIn("test-private-discord-value", body)
        self.assertNotIn("test-private-api-value", body)

    async def test_twelve_requests_use_at_most_four_parsers_and_remove_temporary_files(self):
        gate = asyncio.Event()
        four_started = asyncio.Event()
        active = maximum = count = 0
        paths = []

        async def fake(path, filename, digest, *, kind="paper"):
            nonlocal active, maximum, count
            active += 1
            count += 1
            maximum = max(maximum, active)
            paths.append(path)
            self.assertTrue(path.exists())
            self.assertNotIn(filename, str(path))
            if count == 4:
                four_started.set()
            await gate.wait()
            active -= 1
            return DocumentInfo("Document", "", None, None, digest, "filename")

        with patch.object(parser_service, "_extract_saved_isolated", fake):
            tasks = [
                asyncio.create_task(self._post(filename=f"original-{number}.pdf"))
                for number in range(12)
            ]
            try:
                await asyncio.wait_for(four_started.wait(), 2)
                self.assertEqual(count, 4)
            finally:
                gate.set()
            results = await asyncio.gather(*tasks)
        self.assertEqual([result.status for result in results], [200] * 12)
        self.assertEqual(maximum, 4)
        self.assertTrue(all(not path.exists() for path in paths))

    async def test_request_has_a_finite_deadline(self):
        async def stalled(*args, **kwargs):
            await asyncio.Event().wait()

        with (
            patch.object(parser_service, "PARSER_REQUEST_TIMEOUT_SECONDS", 0.02),
            patch.object(parser_service, "_extract_saved_isolated", stalled),
        ):
            response = await self._post()
        self.assertEqual(response.status, 503)

    async def test_three_presenters_share_four_real_children_without_unbounded_spawning(self):
        actual_spawn = asyncio.create_subprocess_exec
        processes = []
        paths = []
        four_started = asyncio.Event()
        maximum = 0
        script = """
import hashlib, json, sys, time
from pathlib import Path
while not Path(sys.argv[1]).exists():
    time.sleep(0.01)
data = Path(sys.argv[2]).read_bytes()
print(json.dumps({'title': Path(sys.argv[3]).stem, 'text': '', 'doi': None,
    'arxiv_id': None, 'sha256': hashlib.sha256(data).hexdigest(),
    'extraction_method': 'filename', 'warning': None}))
"""
        with tempfile.TemporaryDirectory() as directory:
            release = Path(directory) / "release"

            async def spawn(*args, **kwargs):
                nonlocal maximum
                paths.append(Path(args[-3]))
                process = await actual_spawn(
                    sys.executable, "-I", "-c", script, str(release), args[-3], args[-2], **kwargs
                )
                processes.append(process)
                maximum = max(maximum, sum(child.returncode is None for child in processes))
                if len(processes) == 4:
                    four_started.set()
                return process

            with patch.object(parser_service.asyncio, "create_subprocess_exec", spawn):
                requests = [
                    asyncio.create_task(
                        self._post(
                            b"%PDF-fixture-" + bytes([number]),
                            filename=f"presenter-{number // 4}-paper-{number % 4}.pdf",
                        )
                    )
                    for number in range(12)
                ]
                try:
                    await asyncio.wait_for(four_started.wait(), 3)
                    await asyncio.sleep(0.03)
                    self.assertEqual(len(processes), 4)
                    self.assertTrue(all(child.returncode is None for child in processes))
                finally:
                    release.touch()
                results = await asyncio.gather(*requests)
            self.assertEqual([result.status for result in results], [200] * 12)
            self.assertEqual(len(processes), 12)
            self.assertEqual(maximum, 4)
            self.assertTrue(all(child.returncode is not None for child in processes))
            self.assertTrue(all(not path.exists() for path in paths))
            titles = {(await response.json())["title"] for response in results}
            self.assertEqual(len(titles), 12)

    async def test_cancellation_during_process_creation_reaps_child_before_releasing_request(self):
        actual_spawn = asyncio.create_subprocess_exec
        created = asyncio.Event()
        finish_spawn = asyncio.Event()
        processes = []

        async def spawn(*args, **kwargs):
            process = await actual_spawn(
                sys.executable, "-I", "-c", "import time; time.sleep(10)", **kwargs
            )
            processes.append(process)
            created.set()
            await finish_spawn.wait()
            return process

        with patch.object(parser_service.asyncio, "create_subprocess_exec", spawn):
            extraction = asyncio.create_task(
                parser_service._extract_saved_isolated(Path("unused"), "paper.pdf", "a" * 64)
            )
            await asyncio.wait_for(created.wait(), 2)
            extraction.cancel()
            await asyncio.sleep(0)
            self.assertFalse(extraction.done())
            extraction.cancel()
            await asyncio.sleep(0)
            self.assertFalse(extraction.done())
            finish_spawn.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(extraction, 2)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)

    async def test_repeated_cancellation_waits_for_child_kill_and_reap_before_returning(self):
        actual_spawn = asyncio.create_subprocess_exec
        actual_stop = parser_service._stop_process
        created = asyncio.Event()
        cleanup_started = asyncio.Event()
        finish_cleanup = asyncio.Event()
        processes = []

        async def spawn(*args, **kwargs):
            process = await actual_spawn(
                sys.executable, "-I", "-c", "import time; time.sleep(10)", **kwargs
            )
            processes.append(process)
            created.set()
            return process

        async def stop(process):
            cleanup_started.set()
            await finish_cleanup.wait()
            await actual_stop(process)

        with (
            patch.object(parser_service.asyncio, "create_subprocess_exec", spawn),
            patch.object(parser_service, "_stop_process", stop),
        ):
            extraction = asyncio.create_task(
                parser_service._extract_saved_isolated(Path("unused"), "paper.pdf", "a" * 64)
            )
            try:
                await asyncio.wait_for(created.wait(), 2)
                # Let the spawn coroutine return before cancelling analysis.
                await asyncio.sleep(0.01)
                extraction.cancel()
                await asyncio.wait_for(cleanup_started.wait(), 2)
                extraction.cancel()
                await asyncio.sleep(0)
                self.assertFalse(extraction.done())
                self.assertIsNone(processes[0].returncode)
            finally:
                finish_cleanup.set()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(extraction, 2)
        self.assertEqual(len(processes), 1)
        self.assertIsNotNone(processes[0].returncode)

    async def test_request_deadline_kills_and_reaps_all_four_active_children(self):
        actual_spawn = asyncio.create_subprocess_exec
        processes = []

        async def spawn(*args, **kwargs):
            process = await actual_spawn(
                sys.executable, "-I", "-c", "import time; time.sleep(10)", **kwargs
            )
            processes.append(process)
            return process

        with (
            patch.object(parser_service.asyncio, "create_subprocess_exec", spawn),
            patch.object(parser_service, "PARSER_REQUEST_TIMEOUT_SECONDS", 0.2),
        ):
            responses = await asyncio.gather(*(self._post() for _ in range(4)))
        self.assertEqual([response.status for response in responses], [503] * 4)
        self.assertEqual(len(processes), 4)
        self.assertTrue(all(child.returncode is not None for child in processes))

    async def test_child_timeout_and_output_flood_return_safe_filename_records(self):
        actual_spawn = asyncio.create_subprocess_exec
        for script in [
            "import time; time.sleep(10)",
            "import sys; sys.stdout.write('x' * 1000000)",
        ]:
            processes = []

            async def spawn(*args, _script=script, _processes=processes, **kwargs):
                process = await actual_spawn(sys.executable, "-I", "-c", _script, **kwargs)
                _processes.append(process)
                return process

            with (
                patch.object(parser_service.asyncio, "create_subprocess_exec", spawn),
                patch.object(parser_service, "PARSER_CHILD_TIMEOUT_SECONDS", 0.04),
            ):
                response = await self._post(_pdf())
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["extraction_method"], "filename")
            self.assertTrue(processes)
            self.assertIsNotNone(processes[0].returncode)


class RemoteParserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = b"%PDF-test-payload"
        self.result = asdict(_document(self.data))
        self.handler = lambda request: web.json_response(self.result)
        self.requests = []

        async def handle(request):
            self.requests.append(
                {
                    "path": request.path,
                    "headers": dict(request.headers),
                    "body": await request.read(),
                }
            )
            response = self.handler(request)
            if asyncio.iscoroutine(response):
                response = await response
            return response

        app = web.Application()
        app.router.add_post("/extract", handle)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.url = str(self.client.make_url("/"))

    async def _extract(self, **kwargs):
        return await extract_remote(
            Path("/not-a-real-local-path.pdf"),
            "논문.pdf",
            self.data,
            kwargs.get("url", self.url),
            kind=kwargs.get("kind", "paper"),
        )

    async def test_remote_contract_transmits_only_bytes_and_filename(self):
        with patch.dict(
            os.environ,
            {
                "DISCORD_TOKEN": "test-secret-never-transmit",
                "HTTP_PROXY": "http://invalid.example:1",
            },
        ):
            result = await self._extract()
        self.assertEqual(result, _document(self.data))
        request = self.requests[0]
        self.assertEqual(request["path"], "/extract")
        self.assertEqual(request["body"], self.data)
        self.assertEqual(request["headers"]["X-Filename"], quote("논문.pdf", safe=""))
        self.assertEqual(request["headers"]["X-Document-Kind"], "paper")
        self.assertNotIn("Authorization", request["headers"])
        self.assertNotIn("not-a-real-local-path", json.dumps(request["headers"]))
        self.assertNotIn("test-secret-never-transmit", json.dumps(request["headers"]))

    async def test_document_kind_is_explicit_and_invalid_values_never_reach_parser(self):
        await self._extract(kind="slides")
        self.assertEqual(self.requests[-1]["headers"]["X-Document-Kind"], "slides")
        for kind in ("unknown", "SLIDES", "slides\r\nAuthorization: injected"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                await self._extract(kind=kind)
        self.assertEqual(len(self.requests), 1)

    async def test_missing_unavailable_and_non_root_configurations_fail_closed(self):
        for url in [
            "",
            "file:///tmp/parser",
            self.url + "other",
            self.url + "?secret=1",
            self.url + "#fragment",
            "http://name:password@localhost:8081",
            "http://localhost:bad",
        ]:
            with self.subTest(url=url), self.assertRaises(ParserUnavailable) as raised:
                await self._extract(url=url)
            self.assertEqual(raised.exception.code, "parser_config_invalid")
        with self.assertRaises(ParserUnavailable) as raised:
            await self._extract(url="http://127.0.0.1:1")
        self.assertEqual(raised.exception.code, "parser_unavailable")
        self.assertEqual(self.requests, [])

    async def test_redirects_and_non_json_fail_without_exposing_response_contents(self):
        for handler in [
            lambda request: web.Response(
                status=302, headers={"Location": "http://127.0.0.1:1/private"}
            ),
            lambda request: web.Response(text="private service exception", status=503),
            lambda request: web.Response(text="<html>private</html>", content_type="text/html"),
            lambda request: web.Response(text="{broken", content_type="application/json"),
        ]:
            self.handler = handler
            with self.assertRaises(ParserUnavailable) as raised:
                await self._extract()
            self.assertNotIn("private", str(raised.exception))
            self.assertIn(raised.exception.code, {"parser_rejected", "parser_invalid_response"})

    async def test_rejected_parser_reports_only_status_and_fixed_code(self):
        self.handler = lambda request: web.Response(text="private parser credentials", status=503)
        with self.assertRaises(ParserUnavailable) as raised:
            await self._extract()
        self.assertEqual(raised.exception.code, "parser_rejected")
        self.assertEqual(raised.exception.upstream_status, 503)
        self.assertNotIn("private", str(raised.exception))

    async def test_document_shapes_lengths_and_checksum_are_validated(self):
        changes = [
            {"title": 123},
            {"title": ""},
            {"title": "a" * 513},
            {"text": "a" * 50_001},
            {"doi": "d" * 256},
            {"arxiv_id": "a" * 129},
            {"warning": "w" * 1025},
            {"sha256": "0" * 64},
            {"extraction_method": "execute"},
            {"extra": "unexpected"},
        ]
        for change in changes:
            self.result = {**asdict(_document(self.data)), **change}
            with self.subTest(change=next(iter(change))), self.assertRaises(ParserUnavailable):
                await self._extract()
        self.result = []
        with self.assertRaises(ParserUnavailable):
            await self._extract()

    async def test_declared_and_streamed_response_sizes_are_bounded(self):
        with patch.object(remote_parser, "MAX_PARSER_RESPONSE_BYTES", 128):
            self.handler = lambda request: web.Response(
                body=b"x" * 129, content_type="application/json"
            )
            with self.assertRaises(ParserUnavailable):
                await self._extract()

            async def chunks(request):
                response = web.StreamResponse(headers={"Content-Type": "application/json"})
                await response.prepare(request)
                await response.write(b"x" * 65)
                await response.write(b"y" * 65)
                await response.write_eof()
                return response

            self.handler = chunks
            with self.assertRaises(ParserUnavailable):
                await self._extract()

    async def test_remote_timeout_is_finite(self):
        async def stalled(request):
            await asyncio.sleep(0.2)
            return web.json_response(self.result)

        self.handler = stalled
        with (
            patch.object(remote_parser, "PARSER_TIMEOUT_SECONDS", 0.02),
            self.assertRaises(ParserUnavailable) as raised,
        ):
            await self._extract()
        self.assertEqual(raised.exception.code, "parser_timeout")

from __future__ import annotations

import asyncio
import tempfile
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from seminar_bot.relay import CHUNK_BYTES, RelayClient, RelayProtocolError, validated_request
from seminar_bot.settings import Settings


class _Peer:
    def __init__(self, socket):
        self.socket = socket
        self.messages = asyncio.Queue()

    async def receive(self, timeout=2):
        return await asyncio.wait_for(self.messages.get(), timeout)

    async def receive_json(self, timeout=2):
        return (await self.receive(timeout)).json()

    async def send_json(self, value):
        await self.socket.send_json(value)

    async def send_bytes(self, value):
        await self.socket.send_bytes(value)


class RelayValidationTests(unittest.TestCase):
    def test_presentations_are_read_only_with_bounded_optional_search_and_session_pages(self):
        for path in (
            "/v1/presentations",
            "/v1/presentations?q=",
            "/v1/presentations?page=2",
            "/v1/presentations?q=10.1234%2Fexample&page=100000",
        ):
            self.assertEqual(validated_request("GET", path, {})[1], 0)
        for method, path in (
            ("POST", "/v1/presentations"),
            ("GET", "/v1/presentations?kind=slides"),
            ("GET", "/v1/presentations?q=a&q=b"),
            ("GET", "/v1/presentations?page=100001"),
            ("GET", "/v1/presentations?page=01"),
            ("GET", "/v1/presentations?q=%00"),
            ("GET", "/v1/presentations?target=http://other-host"),
            ("GET", "/v1/presentations?q=" + "a" * 201),
        ):
            with self.subTest(method=method, path=path), self.assertRaises(RelayProtocolError):
                validated_request(method, path, {})

    def test_archive_feed_accepts_bounded_optional_search_and_read_only_filters(self):
        for path in (
            "/v1/archive",
            "/v1/archive?q=",
            "/v1/archive?kind=slides&page=2",
            "/v1/archive?kind=paper&q=10.1234%2Fexample&page=100000",
        ):
            self.assertEqual(validated_request("GET", path, {})[1], 0)
        for method, path in (
            ("POST", "/v1/archive"),
            ("GET", "/v1/archive?kind=unknown"),
            ("GET", "/v1/archive?q=a&q=b"),
            ("GET", "/v1/archive?page=100001"),
            ("GET", "/v1/archive?page=01"),
            ("GET", "/v1/archive?q=%00"),
            ("GET", "/v1/archive?target=http://other-host"),
            ("GET", "/v1/archive?q=" + "a" * 201),
        ):
            with self.subTest(method=method, path=path), self.assertRaises(RelayProtocolError):
                validated_request(method, path, {})

    def test_document_edits_are_limited_to_fixed_routes_and_bounded_bodies(self):
        for method, path, maximum in (
            ("POST", "/v1/sessions/1/papers", 8192),
            ("PATCH", "/v1/sessions/1/documents/2", 8192),
            ("DELETE", "/v1/sessions/1/documents/2", 1024),
        ):
            with self.subTest(method=method):
                headers, limit = validated_request(method, path, {"x-document-version": "a" * 64})
                self.assertEqual(limit, maximum)
                self.assertEqual(headers["x-document-version"], "a" * 64)
        for method, path in (
            ("PUT", "/v1/sessions/1/papers"),
            ("POST", "/v1/sessions/1/documents/2"),
            ("PATCH", "/v1/sessions/1/documents/2?target=http://other-host"),
            ("DELETE", "/v1/documents/2"),
            ("PATCH", "/v1/sessions/1/documents/0"),
            ("DELETE", "/v1/sessions/1/documents/../2"),
        ):
            with self.subTest(method=method, path=path), self.assertRaises(RelayProtocolError):
                validated_request(method, path, {})

    def test_submission_routes_are_small_and_do_not_accept_destinations(self):
        token = "A" * 43
        headers, maximum = validated_request(
            "GET", "/v1/submission/context", {"x-seminar-submission-token": token}
        )
        self.assertEqual(headers["x-seminar-submission-token"], token)
        self.assertEqual(maximum, 0)
        self.assertEqual(validated_request("POST", "/v1/submission/redeem", {})[1], 4096)
        for method, path in (
            ("GET", "/v1/submission/redeem"),
            ("POST", "/v1/submission/context"),
            ("GET", "/v1/submission/context?session=2"),
            ("POST", "/v1/submission/redeem?target=http://other-host"),
        ):
            with self.subTest(method=method, path=path), self.assertRaises(RelayProtocolError):
                validated_request(method, path, {})

    def test_requests_cannot_select_hosts_other_paths_methods_or_duplicate_query_keys(self):
        for method, path in (
            ("GET", "http://other-host/v1/health"),
            ("GET", "//other-host/v1/health"),
            ("GET", "/v1/%68ealth"),
            ("GET", "/v1/sessions/../health"),
            ("GET", "/v1/health#fragment"),
            ("GET", "/v1/health?target=http://other-host"),
            ("GET", "/v1/search?q=one&q=two"),
            ("GET", "/v1/search?q=%ff"),
            ("GET", "/v1/search?q=%00"),
            ("GET", "/v1/search?q=a&page=0"),
            ("GET", "/v1/search?q=a&kind=invalid"),
            ("DELETE", "/v1/sessions/1"),
            ("PUT", "/v1/sessions/1/documents?kind=other"),
            ("POST", "/v1/sessions/1/submit?unexpected=yes"),
        ):
            with self.subTest(method=method, path=path):
                with self.assertRaises(RelayProtocolError):
                    validated_request(method, path, {})

    def test_only_canonical_safe_request_headers_are_accepted(self):
        for headers in (
            {"host": "other-host"},
            {"content-length": "1"},
            {"transfer-encoding": "chunked"},
            {"x-filename": "safe.pdf\r\nx-injected: yes"},
            {"Accept": "a", "accept": "b"},
        ):
            with self.subTest(headers=headers):
                with self.assertRaises(RelayProtocolError):
                    validated_request("GET", "/v1/health", headers)
        headers, maximum = validated_request("GET", "/v1/search?q=research&kind=paper&page=2", {})
        self.assertEqual(headers, {})
        self.assertEqual(maximum, 0)


class RelayIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.download = b"d" * (CHUNK_BYTES * 2 + 13)
        self.request_started = asyncio.Event()
        self.hold_response = asyncio.Event()

        async def local_handler(request):
            data = await request.read()
            self.calls.append((request.method, request.path_qs, dict(request.headers), data))
            self.request_started.set()
            if request.path == "/v1/documents/2/file":
                await self.hold_response.wait()
            if request.path == "/v1/documents/1/file":
                return web.Response(
                    body=self.download,
                    headers={
                        "Content-Type": "application/octet-stream",
                        "Content-Disposition": "attachment; filename=seminar.pdf",
                        "X-Secret-Internal": "never-forward",
                    },
                )
            if request.path == "/v1/documents/3/file":
                raise web.HTTPFound("http://unreachable.invalid/secret")
            return web.json_response({"ok": True, "received": len(data)})

        app = web.Application(client_max_size=21 * 1024 * 1024)
        app.router.add_route("*", "/{path:.*}", local_handler)
        self.api = TestServer(app)
        await self.api.start_server()
        self.settings = Settings(
            token="test-discord-unused",
            data_dir=Path("/tmp"),
            api_token="a" * 64,
            relay_token="b" * 64,
            connect_token="c" * 64,
            api_transport="relay",
            api_port=self.api.port,
        )
        self.worker_ready = asyncio.Queue()
        self.worker_stop = asyncio.Event()

        async def relay_handler(request):
            ws = web.WebSocketResponse(max_msg_size=CHUNK_BYTES + 1)
            await ws.prepare(request)
            await ws.send_json({"type": "connected", "version": 1})
            peer = _Peer(ws)
            await self.worker_ready.put(peer)
            while True:
                message = await ws.receive()
                await peer.messages.put(message)
                if message.type in {
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                }:
                    break
            return ws

        relay_app = web.Application()
        relay_app.router.add_get("/connect", relay_handler)
        self.worker = TestServer(relay_app)
        await self.worker.start_server()
        self.local = aiohttp.ClientSession(trust_env=False)
        self.connector = aiohttp.ClientSession(trust_env=False)
        self.client_ws = await self.connector.ws_connect(self.worker.make_url("/connect"))
        self.server_ws = await self.worker_ready.get()
        self.relay = RelayClient(self.settings)
        self.task = asyncio.create_task(self.relay.serve(self.client_ws, self.local))

    async def asyncTearDown(self):
        self.hold_response.set()
        self.worker_stop.set()
        await self.client_ws.close()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.connector.close()
        await self.local.close()
        await self.worker.close()
        await self.api.close()

    def request(self, method="GET", path="/v1/sessions", **extra):
        return {
            "type": "request",
            "id": str(uuid.uuid4()),
            "method": method,
            "path": path,
            "headers": {
                "authorization": "Bearer " + self.settings.api_token,
                "x-seminar-relay-token": self.settings.relay_token,
                "content-type": "application/octet-stream"
                if method == "PUT"
                else "application/json",
            },
            "body": method != "GET",
            **extra,
        }

    async def begin(self, request):
        await self.server_ws.send_json(request)
        ready = await self.server_ws.receive_json(timeout=2)
        self.assertEqual(ready, {"type": "request_ready", "id": request["id"]})

    async def end_upload(self, request):
        await self.server_ws.send_json({"type": "request_end", "id": request["id"]})
        response = await self.server_ws.receive_json(timeout=2)
        self.assertEqual(response["type"], "response")
        self.assertEqual(response["id"], request["id"])
        return response

    async def read_response(self, request):
        chunks = []
        while True:
            await self.server_ws.send_json({"type": "response_pull", "id": request["id"]})
            message = await self.server_ws.receive(timeout=2)
            if message.type == aiohttp.WSMsgType.BINARY:
                self.assertLessEqual(len(message.data), CHUNK_BYTES)
                chunks.append(message.data)
            else:
                self.assertEqual(message.json(), {"type": "response_end", "id": request["id"]})
                return b"".join(chunks)

    async def assert_closed(self):
        message = await self.server_ws.receive(timeout=2)
        self.assertIn(message.type, {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED})
        await asyncio.gather(self.task, return_exceptions=True)

    async def test_bounded_upload_reaches_fixed_loopback_only_after_request_end(self):
        request = self.request("PUT", "/v1/sessions/1/documents?kind=paper")
        spools = []
        original = tempfile.TemporaryFile

        def tracked_spool(*args, **kwargs):
            spool = original(*args, **kwargs)
            spools.append(spool)
            return spool

        with patch("seminar_bot.relay.tempfile.TemporaryFile", side_effect=tracked_spool):
            await self.begin(request)
            payload = b"p" * CHUNK_BYTES
            for _ in range(2):
                await self.server_ws.send_bytes(payload)
                ack = await self.server_ws.receive_json(timeout=2)
                self.assertEqual(ack, {"type": "request_ack", "id": request["id"]})
            self.assertEqual(self.calls, [])
            self.assertFalse(spools[0].closed)
            response = await self.end_upload(request)
            self.assertEqual(response["status"], 200)
            self.assertIn(b'"received": 131072', await self.read_response(request))
        self.assertTrue(spools[0].closed)
        self.assertEqual(self.calls[0][3], payload * 2)
        self.assertEqual(self.calls[0][1], request["path"])
        self.assertEqual(self.calls[0][2]["Authorization"], "Bearer " + self.settings.api_token)
        self.assertEqual(self.calls[0][2]["x-seminar-relay-token"], self.settings.relay_token)

    async def test_download_requires_each_pull_and_filters_internal_response_headers(self):
        request = self.request(path="/v1/documents/1/file")
        await self.begin(request)
        response = await self.end_upload(request)
        self.assertEqual(set(response["headers"]), {"content-type", "content-disposition"})
        with self.assertRaises(TimeoutError):
            await self.server_ws.receive(timeout=0.03)
        self.assertEqual(await self.read_response(request), self.download)
        # A completed request frees the single slot for the next API call.
        second = self.request(path="/v1/health")
        await self.begin(second)
        await self.end_upload(second)
        self.assertIn(b'"ok": true', await self.read_response(second))

    async def test_invalid_route_or_credentials_never_reach_loopback(self):
        request = self.request(path="http://another-host/v1/health")
        await self.server_ws.send_json(request)
        await self.assert_closed()
        self.assertEqual(self.calls, [])

    async def test_connector_socket_is_not_sufficient_authorization_for_local_api(self):
        request = self.request(headers={"authorization": "Bearer " + self.settings.api_token})
        await self.server_ws.send_json(request)
        await self.assert_closed()
        self.assertEqual(self.calls, [])

    async def test_disconnect_during_upload_closes_and_unlinks_the_anonymous_spool(self):
        request = self.request("PUT", "/v1/sessions/1/documents?kind=paper")
        spools = []
        original = tempfile.TemporaryFile

        def tracked_spool(*args, **kwargs):
            spool = original(*args, **kwargs)
            spools.append(spool)
            return spool

        with patch("seminar_bot.relay.tempfile.TemporaryFile", side_effect=tracked_spool):
            await self.begin(request)
            await self.server_ws.send_bytes(b"partial paper")
            await self.server_ws.receive_json(timeout=2)
            await self.client_ws.close()
            await asyncio.gather(self.task, return_exceptions=True)
        self.assertTrue(spools[0].closed)
        self.assertEqual(self.calls, [])

    async def test_oversized_submit_closes_before_any_local_request(self):
        request = self.request("POST", "/v1/sessions/1/submit")
        await self.begin(request)
        await self.server_ws.send_bytes(b"x" * 4097)
        await self.assert_closed()
        self.assertEqual(self.calls, [])

    async def test_wrong_request_identifier_closes_and_cancels_waiting_http(self):
        request = self.request(path="/v1/documents/2/file")
        await self.begin(request)
        await self.server_ws.send_json({"type": "request_end", "id": request["id"]})
        await asyncio.wait_for(self.request_started.wait(), 2)
        await self.server_ws.send_json({"type": "response_pull", "id": str(uuid.uuid4())})
        await self.assert_closed()

    async def test_local_redirect_is_not_followed(self):
        request = self.request(path="/v1/documents/3/file")
        await self.begin(request)
        response = await self.end_upload(request)
        self.assertEqual(response["status"], 302)
        self.assertEqual(len(self.calls), 1)
        await self.read_response(request)

    async def test_existing_wire_uuid_is_forwarded_to_local_api_without_protocol_change(self):
        request = self.request()
        self.assertNotIn("x-seminar-request-id", request["headers"])
        await self.begin(request)
        response = await self.end_upload(request)
        self.assertEqual(response["status"], 200)
        await self.read_response(request)
        self.assertEqual(self.calls[0][2]["X-Seminar-Request-ID"], request["id"])

    async def test_stalled_upload_timeout_closes_and_cleans_spool(self):
        request = self.request("PUT", "/v1/sessions/1/documents?kind=paper")
        with patch("seminar_bot.relay.REQUEST_TIMEOUT", 0.03):
            await self.begin(request)
            await self.assert_closed()
        self.assertEqual(self.calls, [])

    async def test_malformed_control_and_a_second_request_close_the_connection(self):
        request = self.request("PUT", "/v1/sessions/1/documents?kind=paper")
        await self.begin(request)
        await self.server_ws.send_json(self.request())
        await self.assert_closed()
        self.assertEqual(self.calls, [])

    async def test_response_size_limit_stops_transmission(self):
        request = self.request(path="/v1/documents/1/file")
        await self.begin(request)
        with patch("seminar_bot.relay.MAX_BODY_BYTES", 100):
            await self.end_upload(request)
            await self.server_ws.send_json({"type": "response_pull", "id": request["id"]})
            await self.assert_closed()

    async def test_ws_redirect_never_forwards_the_connector_credential(self):
        redirected = asyncio.Event()
        leaked_headers = []

        async def redirect(request):
            redirected.set()
            raise web.HTTPFound("/target")

        async def target(request):
            leaked_headers.append(dict(request.headers))
            return web.Response()

        app = web.Application()
        app.router.add_get("/redirect", redirect)
        app.router.add_get("/target", target)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        # The production Settings parser requires WSS workers.dev. A loopback
        # test destination isolates redirect behavior from DNS/TLS availability.
        client = RelayClient(replace(self.settings, relay_url=str(server.make_url("/redirect"))))
        with self.assertLogs("seminar_bot.relay", "WARNING") as captured:
            client.start()
            await asyncio.wait_for(redirected.wait(), 2)
            await asyncio.sleep(0.02)
            await client.close()
        self.assertEqual(leaked_headers, [])
        self.assertNotIn(self.settings.connect_token, " ".join(captured.output))
        self.assertNotIn("/redirect", " ".join(captured.output))


if __name__ == "__main__":
    unittest.main()

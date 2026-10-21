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

from seminar_bot import relay as relay_module
from seminar_bot.relay import (
    CHUNK_BYTES,
    CONNECTION_SLOTS,
    RelayClient,
    RelayProtocolError,
    validated_request,
)
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


class RelayPoolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        self.started = asyncio.Queue()
        self.release = asyncio.Event()
        self.ready = asyncio.Queue()
        self.seen_slots = []
        self.connections = []
        original_connection = relay_module._Connection

        def track_connection(*args, **kwargs):
            connection = original_connection(*args, **kwargs)
            self.connections.append(connection)
            return connection

        tracker = patch("seminar_bot.relay._Connection", side_effect=track_connection)
        tracker.start()
        self.addCleanup(tracker.stop)

        async def local_handler(request):
            body = await request.read()
            self.calls.append((request.path_qs, dict(request.headers), body))
            await self.started.put(request.headers["X-Seminar-Request-ID"])
            await self.release.wait()
            return web.json_response({"received": len(body), "request": request.path_qs})

        api_app = web.Application()
        api_app.router.add_route("*", "/{path:.*}", local_handler)
        self.api = TestServer(api_app)
        await self.api.start_server()
        self.addAsyncCleanup(self.api.close)

        async def relay_handler(request):
            slot = request.headers.get("X-Seminar-Relay-Slot")
            self.assertIn(slot, {str(number) for number in range(CONNECTION_SLOTS)})
            self.assertEqual(request.headers.get("Authorization"), "Bearer " + "c" * 64)
            self.seen_slots.append(slot)
            socket = web.WebSocketResponse(max_msg_size=CHUNK_BYTES + 1)
            await socket.prepare(request)
            await socket.send_json({"type": "connected", "version": 1})
            peer = _Peer(socket)
            await self.ready.put((int(slot), peer))
            while True:
                message = await socket.receive()
                await peer.messages.put(message)
                if message.type in {
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                }:
                    break
            return socket

        worker_app = web.Application()
        worker_app.router.add_get("/connect", relay_handler)
        self.worker = TestServer(worker_app)
        await self.worker.start_server()
        self.addAsyncCleanup(self.worker.close)
        self.settings = Settings(
            token="test-discord-unused",
            data_dir=Path("/tmp"),
            api_token="a" * 64,
            relay_token="b" * 64,
            connect_token="c" * 64,
            api_transport="relay",
            api_port=self.api.port,
            relay_url=str(self.worker.make_url("/connect")),
        )
        self.relay = RelayClient(self.settings)
        self.addAsyncCleanup(self.relay.close)
        self.relay.start()
        self.peers = dict(
            await asyncio.gather(
                *(asyncio.wait_for(self.ready.get(), 3) for _ in range(CONNECTION_SLOTS))
            )
        )

    async def asyncTearDown(self):
        self.release.set()
        await self.relay.close()

    def request(self, slot, body=False):
        return {
            "type": "request",
            "id": str(uuid.uuid4()),
            "method": "PUT" if body else "GET",
            "path": f"/v1/sessions/{slot + 1}/documents?kind=paper"
            if body
            else f"/v1/sessions/{slot + 1}",
            "headers": {
                "authorization": "Bearer " + self.settings.api_token,
                "x-seminar-relay-token": self.settings.relay_token,
                "content-type": "application/octet-stream" if body else "application/json",
            },
            "body": body,
        }

    async def begin(self, slot, request, body=None):
        peer = self.peers[slot]
        await peer.send_json(request)
        self.assertEqual(await peer.receive_json(), {"type": "request_ready", "id": request["id"]})
        if body is not None:
            await peer.send_bytes(body)
            self.assertEqual(
                await peer.receive_json(), {"type": "request_ack", "id": request["id"]}
            )

    async def read_response(self, slot, request):
        peer = self.peers[slot]
        response = await peer.receive_json()
        self.assertEqual(response["id"], request["id"])
        self.assertEqual(response["status"], 200)
        chunks = []
        while True:
            await peer.send_json({"type": "response_pull", "id": request["id"]})
            message = await peer.receive()
            if message.type == aiohttp.WSMsgType.BINARY:
                chunks.append(message.data)
            else:
                self.assertEqual(message.json(), {"type": "response_end", "id": request["id"]})
                return b"".join(chunks)

    async def test_pool_forwards_four_uploads_and_two_queries_to_loopback_concurrently(self):
        self.assertEqual(CONNECTION_SLOTS, 6)
        self.assertEqual(set(self.peers), set(range(CONNECTION_SLOTS)))
        requests = [self.request(slot, body=slot < 4) for slot in range(CONNECTION_SLOTS)]
        payloads = [
            bytes([65 + slot]) * (slot + 1) if slot < 4 else b"" for slot in range(CONNECTION_SLOTS)
        ]
        await asyncio.gather(
            *(
                self.begin(slot, request, payloads[slot] if slot < 4 else None)
                for slot, request in enumerate(requests)
            )
        )
        await asyncio.gather(
            *(
                self.peers[slot].send_json({"type": "request_end", "id": request["id"]})
                for slot, request in enumerate(requests)
            )
        )
        # Four uploads and two queries must enter the loopback independently.
        # A shared single-connection client would leave five calls waiting.
        started = await asyncio.wait_for(
            asyncio.gather(*(self.started.get() for _ in range(CONNECTION_SLOTS))), 2
        )
        self.assertEqual(set(started), {request["id"] for request in requests})
        self.assertEqual(len({id(connection.local) for connection in self.connections}), 6)
        for path, headers, body in self.calls:
            slot = int(path.split("/")[3]) - 1
            self.assertEqual(body, payloads[slot])
            self.assertEqual(headers["X-Seminar-Request-ID"], requests[slot]["id"])
            self.assertNotIn("X-Seminar-Relay-Slot", headers)
        self.release.set()
        results = await asyncio.gather(
            *(self.read_response(slot, request) for slot, request in enumerate(requests))
        )
        for slot, result in enumerate(results):
            self.assertIn(f'"received": {len(payloads[slot])}'.encode(), result)

    async def test_protocol_failure_reconnects_only_its_slot_and_other_slots_remain_usable(self):
        failed = self.request(0, body=True)
        await self.begin(0, failed, b"partial")
        await self.peers[0].send_json({"type": "response_pull", "id": str(uuid.uuid4())})
        closed = await self.peers[0].receive()
        self.assertIn(closed.type, {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED})
        requests = {slot: self.request(slot) for slot in range(1, CONNECTION_SLOTS)}
        await asyncio.gather(*(self.begin(slot, request) for slot, request in requests.items()))
        await asyncio.gather(
            *(
                self.peers[slot].send_json({"type": "request_end", "id": request["id"]})
                for slot, request in requests.items()
            )
        )
        await asyncio.wait_for(asyncio.gather(*(self.started.get() for _ in requests)), 2)
        self.release.set()
        await asyncio.gather(
            *(self.read_response(slot, request) for slot, request in requests.items())
        )
        slot, replacement = await asyncio.wait_for(self.ready.get(), 3)
        self.assertEqual(slot, 0)
        self.assertIsNot(replacement, self.peers[0])
        self.assertEqual(sorted(self.seen_slots), ["0", "0", "1", "2", "3", "4", "5"])
        for healthy in range(1, CONNECTION_SLOTS):
            self.assertFalse(self.peers[healthy].socket.closed)

    async def test_close_cancels_all_slots_http_work_and_anonymous_spools(self):
        spools = []
        original = tempfile.TemporaryFile

        def tracked_spool(*args, **kwargs):
            spool = original(*args, **kwargs)
            spools.append(spool)
            return spool

        with patch("seminar_bot.relay.tempfile.TemporaryFile", side_effect=tracked_spool):
            requests = [self.request(slot, body=True) for slot in range(CONNECTION_SLOTS)]
            await asyncio.gather(
                *(self.begin(slot, request, b"partial") for slot, request in enumerate(requests))
            )
            for slot in (0, 1):
                await self.peers[slot].send_json(
                    {"type": "request_end", "id": requests[slot]["id"]}
                )
            await asyncio.wait_for(asyncio.gather(self.started.get(), self.started.get()), 2)
            self.assertEqual(len(spools), CONNECTION_SLOTS)
            self.assertTrue(all(not spool.closed for spool in spools))
            await self.relay.close()
        self.assertTrue(all(spool.closed for spool in spools))
        self.assertIsNone(self.relay._task)
        for connection in self.connections:
            self.assertIsNone(connection.active)
            self.assertEqual(connection.tasks, set())
            self.assertTrue(connection.local.closed)
            self.assertTrue(connection.ws.closed)
        self.assertFalse(
            any(
                task.get_name().startswith("seminar-outbound-relay") for task in asyncio.all_tasks()
            )
        )
        await self.relay.close()

    async def test_direct_run_uses_six_slots_and_close_waits_for_the_whole_pool(self):
        await self.relay.close()
        direct = asyncio.create_task(self.relay.run())
        await asyncio.gather(
            *(asyncio.wait_for(self.ready.get(), 3) for _ in range(CONNECTION_SLOTS))
        )
        with self.assertRaisesRegex(RuntimeError, "already started"):
            self.relay.start()
        await self.relay.close()
        await asyncio.gather(direct, return_exceptions=True)
        self.assertTrue(direct.done())
        self.assertIsNone(self.relay._task)


if __name__ == "__main__":
    unittest.main()

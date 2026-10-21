"""Bounded outbound WebSocket transport for the local seminar API."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import random
import re
import tempfile
from dataclasses import dataclass, field
from typing import BinaryIO
from urllib.parse import parse_qsl, urlsplit

import aiohttp

from .settings import Settings

log = logging.getLogger(__name__)


class RelayProtocolError(Exception):
    """The remote peer violated the limited relay protocol."""


CHUNK_BYTES = 64 * 1024
MAX_BODY_BYTES = 20 * 1024 * 1024
MAX_CONTROL_BYTES = 16 * 1024
REQUEST_TIMEOUT = 70
HANDSHAKE_TIMEOUT = 15
_ID = r"[1-9][0-9]{0,18}"
_UUID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[1-8][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}")
_REQUEST_HEADERS = {
    "authorization",
    "x-seminar-relay-token",
    "accept",
    "content-type",
    "x-filename",
    "idempotency-key",
    "x-seminar-version",
    "x-seminar-submission-token",
    "x-document-version",
}
_RESPONSE_HEADERS = {
    "content-type",
    "content-disposition",
}


def validated_request(method: object, path: object, headers: object) -> tuple[dict[str, str], int]:
    """Permit only the fixed seminar routes before touching the loopback listener."""
    if not isinstance(method, str) or not isinstance(path, str) or len(path) > 2048:
        raise RelayProtocolError("Invalid request")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise RelayProtocolError("Invalid request")
    try:
        parsed = urlsplit(path)
    except ValueError as exc:
        raise RelayProtocolError("Invalid request") from exc
    if parsed.scheme or parsed.netloc or parsed.fragment or "%" in parsed.path:
        raise RelayProtocolError("Invalid request")
    if parsed.path in {"/v1/health", "/v1/sessions", "/v1/submission/context"} or re.fullmatch(
        rf"/v1/sessions/{_ID}|/v1/documents/{_ID}/file", parsed.path
    ):
        allowed_method, query_keys, maximum = "GET", set(), 0
    elif re.fullmatch(rf"/v1/sessions/{_ID}/documents", parsed.path):
        allowed_method, query_keys, maximum = "PUT", {"kind"}, MAX_BODY_BYTES
    elif re.fullmatch(rf"/v1/sessions/{_ID}/submit", parsed.path):
        allowed_method, query_keys, maximum = "POST", set(), 4096
    elif re.fullmatch(rf"/v1/sessions/{_ID}/papers", parsed.path):
        allowed_method, query_keys, maximum = "POST", set(), 8192
    elif re.fullmatch(rf"/v1/sessions/{_ID}/documents/{_ID}", parsed.path):
        if method not in {"PATCH", "DELETE"}:
            raise RelayProtocolError("Invalid request")
        allowed_method, query_keys = method, set()
        maximum = 8192 if method == "PATCH" else 1024
    elif parsed.path == "/v1/submission/redeem":
        allowed_method, query_keys, maximum = "POST", set(), 4096
    elif parsed.path == "/v1/search":
        allowed_method, query_keys, maximum = "GET", {"q", "kind", "page"}, 0
    elif parsed.path == "/v1/archive":
        allowed_method, query_keys, maximum = "GET", {"q", "kind", "page"}, 0
    else:
        raise RelayProtocolError("Invalid request")
    if method != allowed_method or re.search(r"%(?![a-fA-F0-9]{2})", parsed.query):
        raise RelayProtocolError("Invalid request")
    try:
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
            max_num_fields=3,
        )
    except (ValueError, UnicodeError) as exc:
        raise RelayProtocolError("Invalid request") from exc
    query = dict(pairs)
    if len(query) != len(pairs) or not set(query) <= query_keys:
        raise RelayProtocolError("Invalid request")
    if allowed_method == "PUT" and query.get("kind") not in {"slides", "paper"}:
        raise RelayProtocolError("Invalid request")
    if parsed.path == "/v1/search":
        search = query.get("q", "")
        if (
            not search.strip()
            or len(search.encode("utf-8")) > 400
            or any(ord(char) < 32 or ord(char) == 127 for char in search)
            or query.get("kind", "paper") not in {"paper", "slides", "all", "unknown"}
            or not re.fullmatch(r"[1-9][0-9]{0,5}", query.get("page", "1"))
        ):
            raise RelayProtocolError("Invalid request")
    if parsed.path == "/v1/archive":
        search = query.get("q", "")
        page = query.get("page", "1")
        if (
            len(search) > 200
            or len(search.encode("utf-8")) > 400
            or any(ord(char) < 32 or ord(char) == 127 for char in search)
            or query.get("kind", "all") not in {"paper", "slides", "all"}
            or not re.fullmatch(r"[1-9][0-9]{0,5}", page)
            or int(page) > 100000
        ):
            raise RelayProtocolError("Invalid request")
    if not isinstance(headers, dict) or len(headers) > len(_REQUEST_HEADERS):
        raise RelayProtocolError("Invalid request")
    filtered = {}
    for name, value in headers.items():
        if (
            not isinstance(name, str)
            or name.lower() not in _REQUEST_HEADERS
            or name.lower() in filtered
            or not isinstance(value, str)
            or len(value) > 2048
            or any(ord(char) < 32 or ord(char) > 126 for char in value)
        ):
            raise RelayProtocolError("Invalid request")
        filtered[name.lower()] = value
    return filtered, maximum


@dataclass
class _Request:
    request_id: str
    method: str
    path: str
    headers: dict[str, str]
    maximum: int
    body: bool
    spool: BinaryIO | None
    phase: str = "upload"
    received: int = 0
    response_size: int = 0
    pull: asyncio.Event = field(default_factory=asyncio.Event)
    credit: bool = False
    task: asyncio.Task | None = None
    deadline: asyncio.Task | None = None


async def _no_redirect(_session, _context, _params) -> None:
    # aiohttp's WebSocket helper does not expose allow_redirects. This hook runs
    # before following a redirect and prevents forwarding the connector secret.
    raise RelayProtocolError("Redirect refused")


def _control(message: str) -> dict:
    if len(message.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise RelayProtocolError("Control message too large")
    try:
        value = json.loads(message)
    except (ValueError, RecursionError) as exc:
        raise RelayProtocolError("Invalid control message") from exc
    if not isinstance(value, dict):
        raise RelayProtocolError("Invalid control message")
    return value


class RelayClient:
    """Reconnect an authenticated WSS connection without exposing a NAS listener."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("Relay already started")
        self._task = asyncio.create_task(self.run(), name="seminar-outbound-relay")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def run(self) -> None:
        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(_no_redirect)
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT, sock_connect=10, sock_read=60)
        async with (
            aiohttp.ClientSession(
                trust_env=False,
                timeout=aiohttp.ClientTimeout(total=15),
                trace_configs=[trace],
            ) as connector,
            aiohttp.ClientSession(
                trust_env=False,
                timeout=timeout,
                auto_decompress=False,
                connector=aiohttp.TCPConnector(limit=1),
            ) as local,
        ):
            delay = 1.0
            while True:
                connected_at = asyncio.get_running_loop().time()
                try:
                    async with connector.ws_connect(
                        self.settings.relay_url,
                        headers={"Authorization": f"Bearer {self.settings.connect_token}"},
                        heartbeat=15,
                        # aiohttp rejects messages equal to max_msg_size; the
                        # explicit protocol check still caps chunks at 64 KiB.
                        max_msg_size=CHUNK_BYTES + 1,
                        compress=0,
                    ) as ws:
                        await self.serve(ws, local)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Exceptions may embed URLs/headers: keep diagnostics generic.
                    log.warning("Seminar relay disconnected; reconnecting")
                if asyncio.get_running_loop().time() - connected_at > 30:
                    delay = 1.0
                await asyncio.sleep(delay + random.uniform(0, delay / 4))
                delay = min(delay * 2, 30)

    async def serve(self, ws, local: aiohttp.ClientSession) -> None:
        """Keep receiving continuously, including while loopback HTTP is running."""
        connection = _Connection(self.settings, ws, local)
        try:
            await connection.receive()
        finally:
            try:
                # A timeout/forward task may be waiting for receive() to exit
                # inside aiohttp.close(). Complete the handshake before cleanup
                # cancels that task, otherwise its close frame can be lost.
                async with asyncio.timeout(2):
                    await ws.close()
            except (TimeoutError, aiohttp.ClientError):
                pass
            finally:
                await connection.cleanup()


class _Connection:
    def __init__(self, settings: Settings, ws, local: aiohttp.ClientSession):
        self.settings, self.ws, self.local = settings, ws, local
        self.active: _Request | None = None
        self.connected = False
        self.tasks: set[asyncio.Task] = set()

    async def send(self, kind: str, request_id: str, **payload) -> None:
        await self.ws.send_json({"type": kind, "id": request_id, **payload})

    async def cleanup(self) -> None:
        active, self.active = self.active, None
        if active is not None:
            for task in (active.task, active.deadline):
                if task is not None:
                    task.cancel()
            await asyncio.gather(
                *(task for task in (active.task, active.deadline) if task is not None),
                return_exceptions=True,
            )
            if active.spool is not None:
                active.spool.close()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()

    async def receive(self) -> None:
        try:
            async with asyncio.timeout(HANDSHAKE_TIMEOUT):
                message = await self.ws.receive()
                if message.type != aiohttp.WSMsgType.TEXT:
                    raise RelayProtocolError("Missing handshake")
                await self.on_control(_control(message.data))
            async for message in self.ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    await self.on_control(_control(message.data))
                elif message.type == aiohttp.WSMsgType.BINARY:
                    await self.on_binary(message.data)
                else:
                    raise RelayProtocolError("Socket failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            await self.ws.close(code=1008, message=b"Relay protocol failed")
            raise RelayProtocolError("Relay protocol failed") from None

    async def on_control(self, value: dict) -> None:
        kind = value.get("type")
        if not self.connected:
            if (
                value != {"type": "connected", "version": 1}
                or type(value.get("version")) is not int
            ):
                raise RelayProtocolError("Missing handshake")
            self.connected = True
            log.info("Seminar relay connected over WSS")
            return
        if kind == "request":
            await self.begin(value)
            return
        active = self.active
        if active is None or value.get("id") != active.request_id:
            raise RelayProtocolError("Unexpected request identifier")
        if set(value) != {"type", "id"}:
            raise RelayProtocolError("Unexpected control fields")
        if kind == "request_end" and active.phase == "upload":
            active.phase = "local"
            if active.spool is not None:
                active.spool.seek(0)
            active.task = asyncio.create_task(self.forward(active))
            self.tasks.add(active.task)
            active.task.add_done_callback(self.tasks.discard)
        elif kind == "response_pull" and active.phase == "response" and not active.credit:
            active.credit = True
            active.pull.set()
        else:
            raise RelayProtocolError("Unexpected control phase")

    async def begin(self, value: dict) -> None:
        if self.active is not None or set(value) != {
            "type",
            "id",
            "method",
            "path",
            "headers",
            "body",
        }:
            raise RelayProtocolError("Request is already active or malformed")
        request_id = value.get("id")
        if not isinstance(request_id, str) or not _UUID.fullmatch(request_id):
            raise RelayProtocolError("Invalid request identifier")
        headers, maximum = validated_request(value["method"], value["path"], value["headers"])
        if type(value["body"]) is not bool or value["body"] != (maximum > 0):
            raise RelayProtocolError("Invalid body flag")
        if not hmac.compare_digest(
            headers.get("authorization", ""), f"Bearer {self.settings.api_token}"
        ) or not hmac.compare_digest(
            headers.get("x-seminar-relay-token", ""), self.settings.relay_token
        ):
            raise RelayProtocolError("Request authentication failed")
        # Never accept local credentials or a destination from the remote peer.
        headers["authorization"] = f"Bearer {self.settings.api_token}"
        headers["x-seminar-relay-token"] = self.settings.relay_token
        active = _Request(
            request_id,
            value["method"],
            value["path"],
            headers,
            maximum,
            value["body"],
            tempfile.TemporaryFile(mode="w+b") if value["body"] else None,
        )
        self.active = active
        active.deadline = asyncio.create_task(self.expire(active))
        await self.send("request_ready", request_id)

    async def on_binary(self, chunk: bytes) -> None:
        active = self.active
        if (
            active is None
            or active.phase != "upload"
            or active.spool is None
            or not 0 < len(chunk) <= CHUNK_BYTES
            or active.received + len(chunk) > active.maximum
        ):
            raise RelayProtocolError("Unexpected or oversized body")
        # The spool has no pathname and is removed on every exit. Its maximum is
        # fixed before any write; no cloud or permanent archive stores this copy.
        active.spool.write(chunk)
        active.received += len(chunk)
        await self.send("request_ack", active.request_id)

    async def expire(self, active: _Request) -> None:
        await asyncio.sleep(REQUEST_TIMEOUT)
        if self.active is active:
            await self.ws.close(code=1011, message=b"Relay request timed out")

    async def body(self, active: _Request):
        while active.spool is not None:
            chunk = active.spool.read(CHUNK_BYTES)
            if not chunk:
                return
            yield chunk

    async def forward(self, active: _Request) -> None:
        try:
            async with self.local.request(
                active.method,
                f"http://127.0.0.1:{self.settings.api_port}{active.path}",
                # The protocol's existing UUID also correlates local API logs.
                # Do not extend v1's strict wire-header allowlist: old NAS
                # connectors must keep accepting requests from newer Workers.
                headers={**active.headers, "X-Seminar-Request-ID": active.request_id},
                data=self.body(active) if active.body else None,
                allow_redirects=False,
            ) as response:
                if (
                    response.status < 200
                    or response.status in {204, 205, 304}
                    or response.status > 599
                ):
                    raise RelayProtocolError("Invalid local response")
                headers = {
                    name.lower(): value
                    for name, value in response.headers.items()
                    if name.lower() in _RESPONSE_HEADERS
                }
                if any(
                    len(value) > 2048 or any(ord(char) < 32 or ord(char) > 126 for char in value)
                    for value in headers.values()
                ):
                    raise RelayProtocolError("Invalid local response headers")
                active.phase = "response"
                await self.send(
                    "response", active.request_id, status=response.status, headers=headers
                )
                while True:
                    await active.pull.wait()
                    active.pull.clear()
                    chunk = await response.content.read(CHUNK_BYTES)
                    if not chunk:
                        if self.active is active:
                            self.active = None
                        if active.deadline is not None:
                            active.deadline.cancel()
                        if active.spool is not None:
                            active.spool.close()
                        await self.send("response_end", active.request_id)
                        break
                    active.response_size += len(chunk)
                    if active.response_size > MAX_BODY_BYTES:
                        raise RelayProtocolError("Local response too large")
                    active.credit = False
                    await self.ws.send_bytes(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Closing resets both peers' phase and request identifier together.
            await self.ws.close(code=1011, message=b"Local seminar service failed")

"""Private NAS API used exclusively by the authenticated lab website."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import hmac
import json
import logging
import re
import shutil
import sqlite3
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote

from aiohttp import web

from .access import AccessUnavailable, AccessVerifier
from .documents import DocumentInfo, normalize_search, validate_paper_fields
from .remote_parser import ParserUnavailable
from .rotation import read_rotations
from .store import UserError, archive_search_term, local_now
from .submission_access import SubmissionAccess, SubmissionAccessDenied, session_version

log = logging.getLogger(__name__)
WEB_MAX_BYTES = 20 * 1024 * 1024
PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
REQUEST_ID = re.compile(r"[a-f0-9]{8}-[a-f0-9]{4}-[1-8][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}")
STATUS_CODES = {
    400: "invalid_request",
    401: "nas_auth_failed",
    403: "request_not_allowed",
    404: "not_found",
    409: "conflict",
    413: "file_too_large",
    415: "unsupported_media_type",
    429: "rate_limited",
    503: "backend_unavailable",
}
PARSER_ERRORS = {
    "parser_unavailable": "문서 분석 서버에 연결할 수 없습니다. 분석 컨테이너의 상태를 확인해주세요.",
    "parser_timeout": "문서 분석 서버의 응답 시간이 초과되었습니다.",
    "parser_invalid_response": "문서 분석 서버의 응답 형식이 올바르지 않습니다.",
    "parser_rejected": "문서 분석 서버가 요청을 처리하지 못했습니다.",
    "parser_config_invalid": "문서 분석 서버 연결 설정이 올바르지 않습니다.",
}
EXCEPTION_TYPES = {
    RuntimeError: "RuntimeError",
    ValueError: "ValueError",
    TypeError: "TypeError",
    KeyError: "KeyError",
    AttributeError: "AttributeError",
    AssertionError: "AssertionError",
    MemoryError: "MemoryError",
}


def diagnostic_stage(request, stage: str) -> None:
    request["seminar_diagnostics"]["stage"] = stage


class APIError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def check_version(request, row: dict) -> None:
    if request.headers.get("X-Seminar-Version") != session_version(row):
        raise APIError(
            409, "발표 일정이 변경되었습니다. 회차와 발표자를 다시 확인해주세요.", "session_changed"
        )


def document_version(record: dict) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                key: record.get(key)
                for key in (
                    "id",
                    "attachment_id",
                    "upload_key",
                    "uploaded_at",
                    "source_type",
                    "filename",
                    "session_id",
                    "presenter_id",
                    "seminar_date",
                    "uploader_id",
                    "source_channel_id",
                    "title",
                    "venue",
                    "year",
                    "doi",
                    "arxiv_id",
                    "link",
                    "kind",
                    "sha256",
                    "edit_revision",
                )
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def progress(status: dict) -> dict:
    return {
        "slides_count": status["slides"],
        "paper_count": status["papers"],
        "unknown_count": status["unknown"],
        "complete": status["complete"],
    }


def filename_from_header(value: str) -> str:
    if not value or len(value) > 2048 or re.search(r"%(?![0-9A-Fa-f]{2})", value):
        raise APIError(400, "파일 이름을 확인해주세요.")
    try:
        filename = unquote(value, errors="strict")
    except UnicodeDecodeError as exc:
        raise APIError(400, "파일 이름을 확인해주세요.") from exc
    if (
        not filename.strip()
        or len(filename.encode("utf-8")) > 240
        or any(ord(char) < 32 or ord(char) == 127 or char in "/\\" for char in filename)
        or filename in {".", ".."}
    ):
        raise APIError(400, "파일 이름에 경로나 제어 문자를 사용할 수 없습니다.")
    return filename


class SeminarAPI:
    def __init__(self, bot):
        self.bot = bot
        self.submission_access = SubmissionAccess(bot.store)
        self.settings = bot.settings
        self.guild_id = self.settings.api_guild_id
        if not self.settings.api_token or not self.guild_id:
            raise ValueError("웹 API 토큰과 서버 ID가 필요합니다.")
        if not self.settings.parser_url:
            raise ValueError("웹 업로드에는 격리된 문서 분석 서비스가 필요합니다.")
        if self.settings.api_transport in {"vpc", "relay"}:
            if (
                not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.settings.relay_token)
                or self.settings.relay_token == self.settings.api_token
            ):
                raise ValueError("비공개 연결에는 API 토큰과 다른 중계 인증 토큰이 필요합니다.")
            self.access = None
        elif self.settings.api_transport == "access":
            self.access = AccessVerifier(
                self.settings.access_team_domain, self.settings.access_audience
            )
        else:
            raise ValueError("허용되지 않는 웹 API 연결 방식입니다.")
        self.max_bytes = min(WEB_MAX_BYTES, self.settings.max_file_mb * 1024 * 1024)
        self.app = web.Application(middlewares=[self.guard], client_max_size=self.max_bytes)
        self.app.add_routes(
            [
                web.get("/v1/health", self.health),
                web.post("/v1/submission/redeem", self.redeem_submission),
                web.get("/v1/submission/context", self.submission_context),
                web.get("/v1/sessions", self.sessions),
                web.get("/v1/sessions/{session_id:[1-9][0-9]{0,18}}", self.session),
                web.put("/v1/sessions/{session_id:[1-9][0-9]{0,18}}/documents", self.upload),
                web.post("/v1/sessions/{session_id:[1-9][0-9]{0,18}}/submit", self.submit),
                web.post("/v1/sessions/{session_id:[1-9][0-9]{0,18}}/papers", self.manual_paper),
                web.patch(
                    "/v1/sessions/{session_id:[1-9][0-9]{0,18}}/documents/{document_id:[1-9][0-9]{0,18}}",
                    self.edit_document,
                ),
                web.delete(
                    "/v1/sessions/{session_id:[1-9][0-9]{0,18}}/documents/{document_id:[1-9][0-9]{0,18}}",
                    self.delete_document,
                ),
                web.get("/v1/search", self.search),
                web.get("/v1/archive", self.archive),
                web.get("/v1/presentations", self.presentations),
                web.get("/v1/documents/{document_id:[1-9][0-9]{0,18}}/file", self.download),
            ]
        )

    @web.middleware
    async def guard(self, request, handler):
        started = time.monotonic()
        supplied_id = request.headers.get("X-Seminar-Request-ID", "")
        request_id = supplied_id if REQUEST_ID.fullmatch(supplied_id) else str(uuid.uuid4())
        request["seminar_diagnostics"] = {"request_id": request_id, "stage": "nas_auth"}

        def error_response(
            status: int, message: str, code: str, upstream_status=None, exception_type=None
        ):
            diagnostics = {
                **request["seminar_diagnostics"],
                "elapsed_ms": min(600_000, max(0, int((time.monotonic() - started) * 1000))),
            }
            if upstream_status is not None:
                diagnostics["upstream_status"] = upstream_status
            # Only fixed codes/stages and bounded counters are logged. No
            # exception text, credentials, query strings, paths, or filenames.
            log.log(
                logging.ERROR if status >= 500 else logging.WARNING,
                "Seminar API failure %s",
                json.dumps(
                    {
                        "code": code,
                        "status": status,
                        **diagnostics,
                        **({"exception_type": exception_type} if exception_type else {}),
                    }
                ),
            )
            return web.json_response(
                {"error": message, "code": code, "diagnostics": diagnostics},
                status=status,
                headers={**PRIVATE_HEADERS, "X-Seminar-Request-ID": request_id},
            )

        expected = f"Bearer {self.settings.api_token}".encode()
        provided = request.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(expected, provided):
            return error_response(401, "인증이 필요합니다.", "nas_auth_failed")
        try:
            if self.access is not None:
                assertion = request.headers.get("Cf-Access-Jwt-Assertion", "")
                if not await self.access.verify(assertion):
                    raise APIError(401, "서버 간 인증을 확인할 수 없습니다.")
            elif not hmac.compare_digest(
                self.settings.relay_token.encode(),
                request.headers.get("X-Seminar-Relay-Token", "").encode(),
            ):
                raise APIError(401, "비공개 중계 서버 인증을 확인할 수 없습니다.")
            diagnostic_stage(request, "nas_ready")
            if request.path != "/v1/health" and not self.bot.is_ready():
                raise APIError(
                    503,
                    "봇이 Discord에 연결 중입니다. 잠시 후 다시 시도해주세요.",
                    "bot_not_ready",
                )
            diagnostic_stage(request, "nas_auth")
            if "X-Seminar-Submission-Token" in request.headers:
                await self.bot.reload_rotation_file()
                async with self.bot.schedule_lock:
                    self._submission_scope(request)
                    allowed = (
                        request.method == "GET" and request.path == "/v1/submission/context"
                    ) or any(
                        request.method == method and re.fullmatch(pattern, request.path)
                        for method, pattern in (
                            ("GET", r"/v1/sessions/[1-9][0-9]{0,18}"),
                            ("PUT", r"/v1/sessions/[1-9][0-9]{0,18}/documents"),
                            ("POST", r"/v1/sessions/[1-9][0-9]{0,18}/(?:submit|papers)"),
                            ("PATCH", r"/v1/sessions/[1-9][0-9]{0,18}/documents/[1-9][0-9]{0,18}"),
                            ("DELETE", r"/v1/sessions/[1-9][0-9]{0,18}/documents/[1-9][0-9]{0,18}"),
                        )
                    )
                    if not allowed:
                        raise APIError(403, "이 링크로는 본인의 자료만 제출할 수 있습니다.")
                    if request.path != "/v1/submission/context":
                        self._session(request)
            diagnostic_stage(request, "nas_request")
            response = await handler(request)
        except SubmissionAccessDenied as exc:
            response = error_response(401, str(exc), "submission_access_denied")
        except APIError as exc:
            response = error_response(
                exc.status, str(exc), exc.code or STATUS_CODES.get(exc.status, "invalid_request")
            )
        except AccessUnavailable:
            response = error_response(
                503,
                "서버 인증 서비스를 확인할 수 없습니다. 잠시 후 다시 시도해주세요.",
                "access_unavailable",
            )
        except UserError as exc:
            response = error_response(400, str(exc), "invalid_request")
        except web.HTTPException as exc:
            response = error_response(
                exc.status, "허용되지 않는 요청입니다.", "request_not_allowed"
            )
        except ParserUnavailable as exc:
            code = exc.code if exc.code in PARSER_ERRORS else "parser_unavailable"
            response = error_response(503, PARSER_ERRORS[code], code, exc.upstream_status)
        except TimeoutError:
            response = error_response(
                408, "자료 처리 시간이 초과되었습니다. 다시 시도해주세요.", "upload_timeout"
            )
        except ConnectionError:
            response = error_response(
                503, "자료 전송 중 연결이 끊겼습니다. 다시 시도해주세요.", "connection_interrupted"
            )
        except PermissionError:
            response = error_response(
                503,
                "NAS 저장소에 접근할 권한이 없습니다. 컨테이너 사용자와 폴더 ACL을 확인해주세요.",
                "storage_permission_denied",
            )
        except OSError as exc:
            full = exc.errno in {errno.ENOSPC, errno.EDQUOT}
            response = error_response(
                503,
                "NAS 저장 공간 또는 할당량이 부족합니다."
                if full
                else "NAS 저장소에서 파일을 처리하지 못했습니다.",
                "storage_full" if full else "storage_unavailable",
            )
        except sqlite3.Error as exc:
            busy = getattr(exc, "sqlite_errorcode", 0) & 0xFF in {
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            }
            response = error_response(
                503,
                "자료 DB가 다른 작업에 사용 중입니다. 잠시 후 다시 시도해주세요."
                if busy
                else "자료 DB를 읽거나 저장하지 못했습니다.",
                "database_busy" if busy else "database_error",
            )
        except Exception as exc:
            response = error_response(
                503,
                "자료 서비스 내부 오류가 발생했습니다. 요청 ID로 서버 로그를 확인해주세요.",
                "internal_error",
                exception_type=EXCEPTION_TYPES.get(type(exc), "Exception"),
            )
        response.headers.update(PRIVATE_HEADERS)
        response.headers["X-Seminar-Request-ID"] = request_id
        return response

    def config(self) -> dict:
        return self.bot.store.require_config(self.guild_id)

    def _check_upload_deleted(self, key: str) -> None:
        if self.bot.store.web_upload_deleted(self.guild_id, key):
            raise APIError(
                409,
                "이 자료는 삭제되었습니다. 새 파일 또는 새 논문으로 등록해주세요.",
                "document_changed",
            )

    def _require_submission(self, request) -> dict:
        grant = self._submission_scope(request)
        if grant is None:
            raise SubmissionAccessDenied()
        return grant

    async def _json_body(self, request, maximum: int = 8192) -> dict:
        if request.content_type != "application/json":
            raise APIError(415, "JSON 형식으로 요청해주세요.")
        if request.content_length is not None and request.content_length > maximum:
            raise APIError(413, "요청이 너무 큽니다.")
        body = bytearray()
        async with asyncio.timeout(10):
            async for chunk in request.content.iter_chunked(1024):
                body.extend(chunk)
                if len(body) > maximum:
                    raise APIError(413, "요청이 너무 큽니다.")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise APIError(400, "요청 형식을 확인해주세요.") from exc
        if not isinstance(payload, dict):
            raise APIError(400, "요청 형식을 확인해주세요.")
        return payload

    def _editable_document(self, request, session: dict, grant: dict) -> dict:
        try:
            document_id = int(request.match_info["document_id"])
            if document_id > 2**63 - 1:
                raise ValueError
            record = self.bot.store.document(self.guild_id, document_id)
        except (ValueError, UserError) as exc:
            raise APIError(404, "자료를 찾을 수 없습니다.") from exc
        if (
            record["session_id"] != session["id"]
            or record["source_channel_id"] != self.config()["upload_channel_id"]
        ):
            raise APIError(404, "자료를 찾을 수 없습니다.")
        if record["uploader_id"] != grant["presenter_id"]:
            raise APIError(
                409, "직접 등록한 자료만 수정하거나 삭제할 수 있습니다.", "document_not_editable"
            )
        if request.headers.get("X-Document-Version") != document_version(record):
            raise APIError(
                409,
                "자료 정보가 변경되었습니다. 새로고침 후 다시 확인해주세요.",
                "document_changed",
            )
        return record

    def _submission_scope(self, request) -> dict | None:
        if "X-Seminar-Submission-Token" not in request.headers:
            return None
        return self.submission_access.validate(
            request.headers["X-Seminar-Submission-Token"], self.guild_id
        )

    def _session(self, request) -> dict:
        grant = self._submission_scope(request)
        try:
            value = int(request.match_info["session_id"])
            if value > 2**63 - 1:
                raise ValueError
            row = self.bot.store.session(self.guild_id, value)
            if grant is not None and (
                row["presenter_id"] != grant["presenter_id"]
                or grant["scope"] == "session"
                and grant["session_id"] != value
            ):
                raise APIError(403, "이 링크로는 본인의 자료만 제출할 수 있습니다.")
            return row
        except (UserError, ValueError) as exc:
            raise APIError(404, "해당 발표 회차를 찾을 수 없습니다.") from exc

    def status(self, session_id: int) -> dict:
        return self.bot.store.submission(
            self.guild_id, session_id, visible_channel_ids=[self.config()["upload_channel_id"]]
        )

    async def names(self) -> dict[int, str]:
        try:
            rotations = await asyncio.to_thread(
                read_rotations, self.settings.rotation_file, default_guild_id=self.guild_id
            )
            for rotation in rotations:
                if rotation.guild_id == self.guild_id:
                    return {
                        user_id: name
                        for user_id, name in zip(rotation.presenters, rotation.names, strict=True)
                        if name
                    }
        except (UserError, OSError):
            pass
        return {}

    def session_data(self, row: dict, names: dict[int, str]) -> dict:
        guild = self.bot.get_guild(self.guild_id)
        member = guild.get_member(row["presenter_id"]) if guild else None
        name = names.get(row["presenter_id"]) or (member.display_name if member else None)
        return {
            "id": row["id"],
            "seminar_date": row["seminar_date"],
            "presenter_id": str(row["presenter_id"]),
            "presenter_name": name or f"발표자 {row['presenter_id']}",
            "version": session_version(row),
        }

    def document_data(
        self, record: dict, names: dict[int, str], presenter_id: int | None = None
    ) -> dict:
        result = {
            key: record.get(key)
            for key in (
                "id",
                "title",
                "filename",
                "kind",
                "doi",
                "arxiv_id",
                "warning",
                "session_id",
                "seminar_date",
                "uploaded_at",
                "venue",
                "year",
                "link",
            )
        }
        result["source"] = record.get("source_type", "discord")
        result["file_available"] = bool(record.get("file_path"))
        owner_id = record.get("presenter_id")
        presenter_name = ""
        if owner_id is not None:
            guild = self.bot.get_guild(self.guild_id)
            member = guild.get_member(owner_id) if guild else None
            presenter_name = (
                names.get(owner_id)
                or (member.display_name if member else None)
                or f"발표자 {owner_id}"
            )
        result["presenter_name"] = presenter_name
        result["revision"] = document_version(record)
        result["editable"] = record.get("uploader_id") == presenter_id
        return result

    async def health(self, request):
        return web.json_response({"ok": True, "ready": self.bot.is_ready()})

    async def redeem_submission(self, request):
        if request.content_type != "application/json":
            raise APIError(415, "JSON 형식으로 요청해주세요.")
        if request.content_length is not None and request.content_length > 1024:
            raise APIError(413, "요청이 너무 큽니다.")
        body = bytearray()
        async with asyncio.timeout(10):
            async for chunk in request.content.iter_chunked(1024):
                body.extend(chunk)
                if len(body) > 1024:
                    raise APIError(413, "요청이 너무 큽니다.")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise SubmissionAccessDenied() from exc
        if not isinstance(payload, dict) or set(payload) != {"token"}:
            raise SubmissionAccessDenied()
        await self.bot.reload_rotation_file()
        async with self.bot.schedule_lock:
            token, expires_at = self.submission_access.redeem(payload["token"], self.guild_id)
        return web.json_response({"access_token": token, "expires_at": expires_at})

    async def submission_context(self, request):
        async with self.bot.schedule_lock:
            grant = self._submission_scope(request)
            if grant is None:
                raise SubmissionAccessDenied()
            rows = self.submission_access.sessions(grant)
        return web.json_response(
            {
                "session_id": grant["session_id"],
                "version": grant["session_version"],
                "expires_at": grant["expires_at"],
                "sessions": [
                    {
                        "id": row["id"],
                        "seminar_date": row["seminar_date"],
                        "version": session_version(row),
                    }
                    for row in rows
                ],
                "max_file_mb": self.max_bytes // (1024 * 1024),
            }
        )

    async def sessions(self, request):
        await self.bot.reload_rotation_file()
        async with self.bot.schedule_lock:
            config = self.config()
            today = local_now(config).date()
            self.bot.store.ensure_horizon(self.guild_id, today, 20)
            rows = self.bot.store.db.execute(
                "SELECT * FROM sessions WHERE guild_id=? AND seminar_date>=? ORDER BY seminar_date DESC LIMIT 100",
                (self.guild_id, (today - timedelta(days=180)).isoformat()),
            ).fetchall()
            rows = [dict(row) for row in reversed(rows)]
            statuses = {row["id"]: progress(self.status(row["id"])) for row in rows}
        names = await self.names()
        return web.json_response(
            {
                "sessions": [
                    {**self.session_data(row, names), **statuses[row["id"]]} for row in rows
                ],
                "max_file_mb": self.max_bytes // (1024 * 1024),
            }
        )

    async def session(self, request):
        async with self.bot.schedule_lock:
            row = self._session(request)
            status = self.status(row["id"])
            documents = [
                self.bot.store.document(self.guild_id, doc["id"]) for doc in status["documents"]
            ]
        names = await self.names()
        return web.json_response(
            {
                "session": self.session_data(row, names),
                "submission": progress(status),
                "documents": [
                    self.document_data(
                        doc,
                        names,
                        row["presenter_id"]
                        if "X-Seminar-Submission-Token" in request.headers
                        else None,
                    )
                    for doc in documents
                ],
            }
        )

    async def upload(self, request):
        diagnostic_stage(request, "nas_upload")
        kind = request.query.get("kind")
        if kind not in {"slides", "paper"}:
            raise APIError(400, "발표 자료 또는 논문 영역을 선택해주세요.")
        if (
            request.content_type != "application/octet-stream"
            or request.headers.get("Content-Encoding", "identity") != "identity"
        ):
            raise APIError(415, "파일 원본 형식으로 업로드해주세요.")
        filename = filename_from_header(request.headers.get("X-Filename", ""))
        suffix = Path(filename).suffix.casefold()
        if suffix not in {".pdf", ".ppt", ".pptx"} or (kind == "paper" and suffix != ".pdf"):
            raise APIError(
                400,
                "발표 자료는 PDF/PPT/PPTX, 논문은 PDF만 제출할 수 있습니다.",
                "file_type_invalid",
            )
        try:
            key = request.headers.get("Idempotency-Key", "")
            upload_id = uuid.UUID(key)
            if str(upload_id) != key.lower():
                raise ValueError
            key = str(upload_id)
        except ValueError as exc:
            raise APIError(
                400, "업로드 ID가 올바르지 않습니다. 파일을 다시 선택해주세요.", "upload_id_invalid"
            ) from exc
        if request.content_length is not None and request.content_length > self.max_bytes:
            raise APIError(413, "파일 크기 제한을 초과했습니다.")
        async with self.bot.schedule_lock:
            original = self._session(request)
            check_version(request, original)
            self._check_upload_deleted(key)
        directory = (
            self.settings.data_dir / "files" / str(self.guild_id)
            if kind == "slides"
            else Path(tempfile.gettempdir()) / "seminar-paper-uploads"
        )
        diagnostic_stage(request, "nas_storage")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if shutil.disk_usage(directory).free < self.max_bytes + 4 * 1024 * 1024:
            raise APIError(
                503,
                "임시 처리 또는 보관 공간이 부족합니다. 관리자에게 문의해주세요.",
                "storage_low_space",
            )
        path = directory / f"web-{uuid.uuid4()}{suffix}"
        saved = False
        try:
            diagnostic_stage(request, "nas_upload")
            async with self.bot.upload_slots:
                async with asyncio.timeout(65):
                    length = 0
                    digest = hashlib.sha256()
                    diagnostic_stage(request, "nas_storage")
                    with path.open("xb") as stream:
                        diagnostic_stage(request, "nas_receive")
                        async for chunk in request.content.iter_chunked(64 * 1024):
                            length += len(chunk)
                            request["seminar_diagnostics"]["uploaded_bytes"] = length
                            if length > self.max_bytes:
                                raise APIError(413, "파일 크기 제한을 초과했습니다.")
                            digest.update(chunk)
                            await asyncio.to_thread(stream.write, chunk)
                    if not length:
                        raise APIError(400, "빈 파일은 제출할 수 없습니다.", "file_empty")
                    diagnostic_stage(request, "nas_storage")
                    with path.open("rb") as prefix_file:
                        prefix = prefix_file.read(1024)
                    valid_header = (
                        (suffix == ".pdf" and b"%PDF-" in prefix)
                        or (
                            suffix == ".ppt"
                            and prefix.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
                        )
                        or (suffix == ".pptx" and prefix.startswith(b"PK\x03\x04"))
                    )
                    if not valid_header:
                        raise APIError(
                            400,
                            "파일 확장자와 실제 문서 형식이 일치하지 않습니다.",
                            "file_type_invalid",
                        )
                    diagnostic_stage(request, "nas_store")
                    async with self.bot.schedule_lock:
                        self._check_upload_deleted(key)
                        existing = self.bot.store.web_upload(self.guild_id, key)
                        if existing:
                            check_version(request, self._session(request))
                            if (
                                existing["session_id"] != original["id"]
                                or existing["source_channel_id"]
                                != self.config()["upload_channel_id"]
                                or existing["kind"] != kind
                                or existing["filename"] != filename
                                or existing["sha256"] != digest.hexdigest()
                            ):
                                raise APIError(
                                    409,
                                    "업로드 ID가 다른 파일에 이미 사용되었습니다. 파일을 다시 선택해주세요.",
                                )
                            status = self.status(original["id"])
                            names = await self.names()
                            return web.json_response(
                                {
                                    "document": self.document_data(
                                        existing,
                                        names,
                                        original["presenter_id"]
                                        if "X-Seminar-Submission-Token" in request.headers
                                        else None,
                                    ),
                                    "submission": progress(status),
                                    "duplicate": True,
                                }
                            )
                    diagnostic_stage(request, "nas_storage")
                    data = await asyncio.to_thread(path.read_bytes)
                    diagnostic_stage(request, "nas_parse")
                    info = await self.bot.extract_document(path, filename, data, kind=kind)
                    diagnostic_stage(request, "nas_store")
                    async with self.bot.schedule_lock:
                        current = self._session(request)
                        check_version(request, current)
                        self._check_upload_deleted(key)
                        if (
                            current["presenter_id"] != original["presenter_id"]
                            or current["seminar_date"] != original["seminar_date"]
                        ):
                            raise APIError(
                                409,
                                "업로드 중 일정이 바뀌었습니다. 회차를 다시 확인해주세요.",
                                "session_changed",
                            )
                        existing = self.bot.store.web_upload(self.guild_id, key)
                        if existing:
                            if (
                                existing["sha256"] != info.sha256
                                or existing["source_channel_id"]
                                != self.config()["upload_channel_id"]
                                or existing["session_id"] != current["id"]
                                or existing["kind"] != kind
                                or existing["filename"] != filename
                            ):
                                raise APIError(
                                    409,
                                    "업로드 ID가 이미 사용되었습니다. 파일을 다시 선택해주세요.",
                                )
                            record = existing
                        else:
                            # Negative IDs form a separate namespace from Discord snowflakes.
                            attachment_id = -(upload_id.int % (2**63 - 1) + 1)
                            record_id, _ = self.bot.store.add_document(
                                self.guild_id,
                                attachment_id,
                                current["presenter_id"],
                                info,
                                filename,
                                str(path.relative_to(self.settings.data_dir))
                                if kind == "slides"
                                else "",
                                self.settings.submission_url(current["id"]),
                                datetime.now(UTC),
                                kind=kind,
                                classification_reason="웹 제출 영역에서 지정한 구분",
                                session_id=current["id"],
                                source_type="web",
                                upload_key=key,
                            )
                            saved = True
                            record = self.bot.store.document(self.guild_id, record_id)
                        status = self.status(current["id"])
                    names = await self.names()
                    return web.json_response(
                        {
                            "document": self.document_data(
                                record,
                                names,
                                current["presenter_id"]
                                if "X-Seminar-Submission-Token" in request.headers
                                else None,
                            ),
                            "submission": progress(status),
                            "duplicate": not saved,
                        },
                        status=201 if saved else 200,
                    )
        finally:
            if not saved or kind == "paper":
                path.unlink(missing_ok=True)

    async def submit(self, request):
        # No uploader identity is accepted: the lab's shared website login authorizes
        # submitting for the selected session, not proof of the presenter's identity.
        if request.content_length is not None and request.content_length > 4096:
            raise APIError(413, "요청이 너무 큽니다.")
        length = 0
        async with asyncio.timeout(10):
            async for chunk in request.content.iter_chunked(4096):
                length += len(chunk)
                if length > 4096:
                    raise APIError(413, "요청이 너무 큽니다.")
        async with self.bot.schedule_lock:
            row = self._session(request)
            check_version(request, row)
            status = self.status(row["id"])
            if not status["complete"]:
                return web.json_response(
                    {
                        "error": "발표 자료 1개 이상과 서로 다른 논문 3편 이상(PDF 또는 직접 입력)이 필요합니다. 등록한 자료는 저장되었습니다.",
                        "submission": progress(status),
                    },
                    status=409,
                )
            signature = hashlib.sha256(
                repr(
                    (
                        row["presenter_id"],
                        row["seminar_date"],
                        [(d["id"], document_version(d)) for d in status["documents"]],
                    )
                ).encode()
            ).hexdigest()
            notifications_disabled = not bool(self.config().get("notifications_enabled", False))
            notified = (
                not notifications_disabled
                and self.bot.store.submission_receipt(row["id"]) == signature
            )
            if not notifications_disabled and not notified:
                try:
                    message_id = await self.bot.notify_web_submission(row, status)
                    self.bot.store.mark_submission_receipt(row["id"], signature, message_id)
                    notified = True
                except Exception:
                    log.exception(
                        "Could not send web submission notification for session %s", row["id"]
                    )
        names = await self.names()
        response = {
            "session": self.session_data(row, names),
            "submission": progress(status),
            "notification_sent": notified,
            "notifications_disabled": notifications_disabled,
        }
        if not notified and not notifications_disabled:
            response["notification_warning"] = (
                "자료 제출은 완료됐지만 Discord 알림을 보내지 못했습니다. 다시 제출을 눌러 알림을 재시도할 수 있습니다."
            )
        return web.json_response(response)

    async def manual_paper(self, request):
        payload = await self._json_body(request)
        try:
            fields = validate_paper_fields(payload)
            key = str(uuid.UUID(request.headers.get("Idempotency-Key", "")))
            if request.headers.get("Idempotency-Key", "").lower() != key:
                raise ValueError("업로드 ID를 확인해주세요.")
        except ValueError as exc:
            raise APIError(400, str(exc) or "논문 정보를 확인해주세요.") from exc
        digest = hashlib.sha256(
            json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        async with self.bot.schedule_lock:
            grant = self._require_submission(request)
            row = self._session(request)
            check_version(request, row)
            self._check_upload_deleted(key)
            existing = self.bot.store.web_upload(self.guild_id, key)
            if existing:
                if (
                    existing["session_id"] != row["id"]
                    or existing["source_channel_id"] != self.config()["upload_channel_id"]
                    or existing["source_type"] != "manual"
                    or existing["sha256"] != digest
                ):
                    raise APIError(
                        409,
                        "업로드 ID가 다른 자료에 사용되었습니다. 다시 등록해주세요.",
                        "document_changed",
                    )
                record = existing
            else:
                record_id, _ = self.bot.store.add_document(
                    self.guild_id,
                    -(uuid.UUID(key).int % (2**63 - 1) + 1),
                    grant["presenter_id"],
                    DocumentInfo(fields["title"], "", fields["doi"], None, digest, "manual"),
                    "manual-paper.pdf",
                    "",
                    self.settings.submission_url(row["id"]),
                    datetime.now(UTC),
                    kind="paper",
                    classification_reason="발표자가 직접 입력한 논문 정보",
                    session_id=row["id"],
                    source_type="manual",
                    upload_key=key,
                    venue=fields["venue"],
                    year=fields["year"],
                    link=fields["link"],
                )
                record = self.bot.store.document(self.guild_id, record_id)
            status = self.status(row["id"])
        return web.json_response(
            {
                "document": self.document_data(record, await self.names(), grant["presenter_id"]),
                "submission": progress(status),
                "duplicate": bool(existing),
            },
            status=200 if existing else 201,
        )

    async def edit_document(self, request):
        payload = await self._json_body(request)
        async with self.bot.schedule_lock:
            grant = self._require_submission(request)
            row = self._session(request)
            check_version(request, row)
            record = self._editable_document(request, row, grant)
            if record["kind"] not in {"paper", "slides"}:
                raise APIError(
                    409, "먼저 발표 자료 또는 논문으로 분류해주세요.", "document_not_editable"
                )
            try:
                fields = validate_paper_fields(payload, slides=record["kind"] == "slides")
            except ValueError as exc:
                raise APIError(400, str(exc)) from exc
            record = self.bot.store.update_document_metadata(self.guild_id, record["id"], fields)
            status = self.status(row["id"])
        return web.json_response(
            {
                "document": self.document_data(record, await self.names(), grant["presenter_id"]),
                "submission": progress(status),
            }
        )

    async def delete_document(self, request):
        if await self._json_body(request, 1024) != {}:
            raise APIError(400, "삭제 요청 형식을 확인해주세요.")
        async with self.bot.schedule_lock:
            grant = self._require_submission(request)
            row = self._session(request)
            check_version(request, row)
            record = self._editable_document(request, row, grant)
            quarantined = None
            path = None
            if record["file_path"] and not self.bot.store.other_documents_use_file(
                record["id"], record["file_path"]
            ):
                path = (self.settings.data_dir / record["file_path"]).resolve()
                if not path.is_relative_to((self.settings.data_dir / "files").resolve()):
                    raise APIError(
                        503, "보관 파일 경로를 확인할 수 없습니다. 관리자에게 문의해주세요."
                    )
                if path.exists():
                    if not path.is_file():
                        raise APIError(
                            503, "보관 파일을 확인할 수 없습니다. 관리자에게 문의해주세요."
                        )
                    quarantined = path.with_name(".deleting-" + str(uuid.uuid4()))
                    try:
                        path.replace(quarantined)
                    except OSError as exc:
                        raise APIError(
                            503, "보관 파일을 삭제할 수 없습니다. 저장 공간 권한을 확인해주세요."
                        ) from exc
            try:
                self.bot.store.delete_document(self.guild_id, record["id"])
            except Exception:
                if quarantined is not None:
                    quarantined.replace(path)
                raise
            if quarantined is not None:
                try:
                    quarantined.unlink()
                except OSError:
                    log.error("Could not remove quarantined seminar archive file")
                    raise APIError(
                        503,
                        "자료 기록은 삭제됐지만 보관 파일 정리가 필요합니다. 관리자에게 문의해주세요.",
                    ) from None
            status = self.status(row["id"])
        return web.json_response({"submission": progress(status)})

    async def search(self, request):
        try:
            page = int(request.query.get("page", "1"))
            if not 1 <= page <= 100000:
                raise ValueError
        except ValueError as exc:
            raise APIError(400, "검색 페이지가 올바르지 않습니다.") from exc
        kind = request.query.get("kind", "paper")
        if kind not in {"paper", "slides", "unknown", "all"}:
            raise APIError(400, "자료 구분이 올바르지 않습니다.")
        async with self.bot.schedule_lock:
            rows, total = self.bot.store.search(
                self.guild_id,
                request.query.get("q", ""),
                page,
                page_size=10,
                visible_channel_ids=[self.config()["upload_channel_id"]],
                kind=None if kind == "all" else kind,
            )
        names = await self.names()
        return web.json_response(
            {
                "documents": [self.document_data(row, names) for row in rows],
                "total": total,
                "page": page,
            }
        )

    async def archive(self, request):
        for key in request.query:
            if key not in {"kind", "q", "page"} or len(request.query.getall(key)) != 1:
                raise APIError(400, "요청 매개변수를 확인해주세요.")
        kind = request.query.get("kind", "all")
        if kind not in {"all", "slides", "paper"}:
            raise APIError(400, "발표 자료 또는 논문을 선택해주세요.")
        raw_page = request.query.get("page", "1")
        if not re.fullmatch(r"[1-9][0-9]{0,5}", raw_page) or int(raw_page) > 100000:
            raise APIError(400, "조회 페이지를 확인해주세요.")
        page = int(raw_page)
        async with self.bot.schedule_lock:
            rows, total = self.bot.store.archive(
                self.guild_id,
                request.query.get("q", ""),
                kind=None if kind == "all" else kind,
                page=page,
                page_size=20,
                visible_channel_ids=[self.config()["upload_channel_id"]],
            )
        names = await self.names()
        return web.json_response(
            {
                "documents": [self.document_data(row, names) for row in rows],
                "total": total,
                "page": page,
                "page_size": 20,
            }
        )

    async def presentations(self, request):
        for key in request.query:
            if key not in {"q", "page"} or len(request.query.getall(key)) != 1:
                raise APIError(400, "요청 매개변수를 확인해주세요.")
        raw_page = request.query.get("page", "1")
        if not re.fullmatch(r"[1-9][0-9]{0,5}", raw_page) or int(raw_page) > 100000:
            raise APIError(400, "조회 페이지를 확인해주세요.")
        query = request.query.get("q", "")
        term = archive_search_term(query)
        names = await self.names()
        display_names = dict(names)
        guild = self.bot.get_guild(self.guild_id)
        if guild is not None:
            for member in getattr(guild, "members", ()):
                display_names.setdefault(member.id, member.display_name)
        matching_presenters = (
            [user_id for user_id, name in display_names.items() if term in normalize_search(name)]
            if term
            else []
        )
        async with self.bot.schedule_lock:
            config = self.config()
            rows, total = self.bot.store.presentations(
                self.guild_id,
                through=local_now(config).date(),
                q=query,
                page=int(raw_page),
                page_size=10,
                visible_channel_ids=[config["upload_channel_id"]],
                presenter_ids=matching_presenters,
            )
        return web.json_response(
            {
                "presentations": [
                    {
                        "session": self.session_data(row["session"], names),
                        "documents": [
                            self.document_data(document, names) for document in row["documents"]
                        ],
                        "slides_count": sum(
                            document["kind"] == "slides" for document in row["documents"]
                        ),
                        "paper_count": sum(
                            document["kind"] == "paper" for document in row["documents"]
                        ),
                    }
                    for row in rows
                ],
                "total": total,
                "page": int(raw_page),
                "page_size": 10,
            }
        )

    async def download(self, request):
        try:
            document_id = int(request.match_info["document_id"])
            if document_id > 2**63 - 1:
                raise ValueError
            record = self.bot.store.document(self.guild_id, document_id)
        except (UserError, ValueError) as exc:
            raise APIError(404, "자료를 찾을 수 없습니다.") from exc
        if record["source_channel_id"] != self.config()["upload_channel_id"]:
            raise APIError(404, "자료를 찾을 수 없습니다.")
        if not record["file_path"]:
            raise APIError(404, "논문 PDF 원본은 보관하지 않습니다. 제목 또는 DOI로 확인해주세요.")
        path = (self.settings.data_dir / record["file_path"]).resolve()
        if (
            not path.is_relative_to((self.settings.data_dir / "files").resolve())
            or not path.is_file()
        ):
            raise APIError(404, "보관 파일을 찾을 수 없습니다.")
        filename = filename_from_header(quote(record["filename"], safe=""))
        return web.FileResponse(
            path,
            headers={
                "Content-Type": "application/octet-stream",
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename, safe='')}",
                **PRIVATE_HEADERS,
            },
        )

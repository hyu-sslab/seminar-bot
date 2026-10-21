"""Private NAS API used exclusively by the authenticated lab website."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import shutil
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, unquote

from aiohttp import web

from .access import AccessUnavailable, AccessVerifier
from .rotation import read_rotations
from .store import UserError, local_now

log = logging.getLogger(__name__)
WEB_MAX_BYTES = 20 * 1024 * 1024
PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}


class APIError(Exception):
    def __init__(self, status: int, message: str, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def session_version(row: dict) -> str:
    return hashlib.sha256(
        f"{row['id']}:{row['seminar_date']}:{row['presenter_id']}".encode()
    ).hexdigest()


def check_version(request, row: dict) -> None:
    if request.headers.get("X-Seminar-Version") != session_version(row):
        raise APIError(
            409, "발표 일정이 변경되었습니다. 회차와 발표자를 다시 확인해주세요.", "session_changed"
        )


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
        self.settings = bot.settings
        self.guild_id = self.settings.api_guild_id
        if not self.settings.api_token or not self.guild_id:
            raise ValueError("웹 API 토큰과 서버 ID가 필요합니다.")
        if not self.settings.parser_url:
            raise ValueError("웹 업로드에는 격리된 문서 분석 서비스가 필요합니다.")
        if self.settings.api_transport == "vpc":
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
                web.get("/v1/sessions", self.sessions),
                web.get("/v1/sessions/{session_id:[1-9][0-9]{0,18}}", self.session),
                web.put("/v1/sessions/{session_id:[1-9][0-9]{0,18}}/documents", self.upload),
                web.post("/v1/sessions/{session_id:[1-9][0-9]{0,18}}/submit", self.submit),
                web.get("/v1/search", self.search),
                web.get("/v1/documents/{document_id:[1-9][0-9]{0,18}}/file", self.download),
            ]
        )

    @web.middleware
    async def guard(self, request, handler):
        expected = f"Bearer {self.settings.api_token}".encode()
        provided = request.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(expected, provided):
            return web.json_response(
                {"error": "인증이 필요합니다."}, status=401, headers=PRIVATE_HEADERS
            )
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
            if request.path != "/v1/health" and not self.bot.is_ready():
                raise APIError(503, "봇이 Discord에 연결 중입니다. 잠시 후 다시 시도해주세요.")
            response = await handler(request)
        except APIError as exc:
            payload = {"error": str(exc)}
            if exc.code:
                payload["code"] = exc.code
            response = web.json_response(payload, status=exc.status)
        except AccessUnavailable:
            response = web.json_response(
                {"error": "서버 인증 서비스를 확인할 수 없습니다. 잠시 후 다시 시도해주세요."},
                status=503,
            )
        except UserError as exc:
            response = web.json_response({"error": str(exc)}, status=400)
        except web.HTTPException as exc:
            response = web.json_response({"error": "허용되지 않는 요청입니다."}, status=exc.status)
        except (TimeoutError, ConnectionError):
            response = web.json_response(
                {"error": "전송 시간이 초과되었습니다. 다시 시도해주세요."}, status=408
            )
        except Exception:
            # No bearer headers, file contents, or original exceptions in HTTP responses.
            log.exception("Seminar API request failed")
            response = web.json_response(
                {"error": "자료 서비스를 사용할 수 없습니다. 잠시 후 다시 시도해주세요."},
                status=503,
            )
        response.headers.update(PRIVATE_HEADERS)
        return response

    def config(self) -> dict:
        return self.bot.store.require_config(self.guild_id)

    def _session(self, request) -> dict:
        try:
            value = int(request.match_info["session_id"])
            if value > 2**63 - 1:
                raise ValueError
            return self.bot.store.session(self.guild_id, value)
        except (UserError, ValueError) as exc:
            raise APIError(404, "해당 발표 회차를 찾을 수 없습니다.") from exc

    def status(self, session_id: int) -> dict:
        return self.bot.store.submission(
            self.guild_id, session_id, visible_channel_ids=[self.config()["upload_channel_id"]]
        )

    async def names(self) -> dict[int, str]:
        try:
            rotations = await asyncio.to_thread(read_rotations, self.settings.rotation_file)
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

    def document_data(self, record: dict, names: dict[int, str]) -> dict:
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
            )
        }
        result["source"] = record.get("source_type", "discord")
        result["file_available"] = bool(record.get("file_path"))
        result["presenter_name"] = names.get(record.get("presenter_id"), "")
        return result

    async def health(self, request):
        return web.json_response({"ok": True, "ready": self.bot.is_ready()})

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
                "documents": [self.document_data(doc, names) for doc in documents],
            }
        )

    async def upload(self, request):
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
            raise APIError(400, "발표 자료는 PDF/PPT/PPTX, 논문은 PDF만 제출할 수 있습니다.")
        try:
            key = request.headers.get("Idempotency-Key", "")
            upload_id = uuid.UUID(key)
            if str(upload_id) != key.lower():
                raise ValueError
            key = str(upload_id)
        except ValueError as exc:
            raise APIError(400, "업로드 ID가 올바르지 않습니다. 파일을 다시 선택해주세요.") from exc
        if request.content_length is not None and request.content_length > self.max_bytes:
            raise APIError(413, "파일 크기 제한을 초과했습니다.")
        async with self.bot.schedule_lock:
            original = self._session(request)
            check_version(request, original)
        directory = (
            self.settings.data_dir / "files" / str(self.guild_id)
            if kind == "slides"
            else Path(tempfile.gettempdir()) / "seminar-paper-uploads"
        )
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if shutil.disk_usage(directory).free < self.max_bytes + 4 * 1024 * 1024:
            raise APIError(503, "임시 처리 또는 보관 공간이 부족합니다. 관리자에게 문의해주세요.")
        path = directory / f"web-{uuid.uuid4()}{suffix}"
        saved = False
        try:
            async with self.bot.upload_slots:
                async with asyncio.timeout(65):
                    length = 0
                    digest = hashlib.sha256()
                    with path.open("xb") as stream:
                        async for chunk in request.content.iter_chunked(64 * 1024):
                            length += len(chunk)
                            if length > self.max_bytes:
                                raise APIError(413, "파일 크기 제한을 초과했습니다.")
                            digest.update(chunk)
                            await asyncio.to_thread(stream.write, chunk)
                    if not length:
                        raise APIError(400, "빈 파일은 제출할 수 없습니다.")
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
                        raise APIError(400, "파일 확장자와 실제 문서 형식이 일치하지 않습니다.")
                    async with self.bot.schedule_lock:
                        existing = self.bot.store.web_upload(self.guild_id, key)
                        if existing:
                            check_version(request, self._session(request))
                            if (
                                existing["session_id"] != original["id"]
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
                                    "document": self.document_data(existing, names),
                                    "submission": progress(status),
                                    "duplicate": True,
                                }
                            )
                    data = await asyncio.to_thread(path.read_bytes)
                    info = await self.bot.extract_document(path, filename, data)
                    async with self.bot.schedule_lock:
                        current = self._session(request)
                        check_version(request, current)
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
                            "document": self.document_data(record, names),
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
                        "error": "발표 자료 1개 이상과 서로 다른 논문 PDF 3편 이상이 필요합니다. 업로드한 파일은 저장되었습니다.",
                        "submission": progress(status),
                    },
                    status=409,
                )
            signature = hashlib.sha256(
                repr(
                    (
                        row["presenter_id"],
                        row["seminar_date"],
                        [(d["id"], d["kind"]) for d in status["documents"]],
                    )
                ).encode()
            ).hexdigest()
            notified = self.bot.store.submission_receipt(row["id"]) == signature
            if not notified:
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
        }
        if not notified:
            response["notification_warning"] = (
                "자료 제출은 완료됐지만 Discord 알림을 보내지 못했습니다. 다시 제출을 눌러 알림을 재시도할 수 있습니다."
            )
        return web.json_response(response)

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

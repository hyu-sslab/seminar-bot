"""Document parsing service with no NAS mounts, credentials, or Internet access.

Run only on the dedicated internal Docker network. Each extraction uses a
short-lived subprocess with its own memory/CPU limits and a minimal environment.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
import tempfile
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote

from aiohttp import web

from .documents import MAX_FILE_BYTES, DocumentInfo
from .remote_parser import MAX_PARSER_RESPONSE_BYTES, validated_document

MAX_PARSER_FILE_BYTES = MAX_FILE_BYTES
PARSER_REQUEST_TIMEOUT_SECONDS = 40
PARSER_CHILD_TIMEOUT_SECONDS = 25
MAX_CONCURRENT_PARSERS = 4
_SEMAPHORE = web.AppKey("parser_semaphore", asyncio.Semaphore)
_SAFE_ENV = {
    "PATH": os.defpath,
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TMPDIR": "/tmp",
}
_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def _json(payload: object, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status, headers=_HEADERS)


def _filename(raw: str) -> str:
    if re.search(r"%(?![0-9a-fA-F]{2})", raw):
        raise ValueError("Invalid encoded filename")
    name = unquote(raw, encoding="utf-8", errors="strict")
    if (
        not name
        or len(name.encode("utf-8")) > 240
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
        or "/" in name
        or "\\" in name
        or Path(name).suffix.casefold() not in {".pdf", ".ppt", ".pptx"}
    ):
        raise ValueError("Invalid document filename")
    return name


async def _wait_cleanup(task: asyncio.Task) -> None:
    interrupted = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            interrupted = True
    task.result()
    if interrupted:
        raise asyncio.CancelledError


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        try:
            if hasattr(os, "killpg"):
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
    # Drain the killed child's bounded pipe so asyncio's flow control cannot
    # leave process.wait() blocked when a parser floods stdout.
    if process.stdout is not None:
        while await process.stdout.read(64 * 1024):
            pass
    await process.wait()


async def _stop(process: asyncio.subprocess.Process) -> None:
    # Repeated request cancellation must not interrupt kill/drain/wait and
    # release the semaphore while an old child is still consuming resources.
    await _wait_cleanup(asyncio.create_task(_stop_process(process)))


async def _extract_saved_isolated(
    path: Path, filename: str, digest: str, *, kind: str = "paper"
) -> DocumentInfo:
    spawning = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-m",
            "seminar_bot.extract_worker",
            str(path),
            filename,
            kind,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=_SAFE_ENV.copy(),
            start_new_session=True,
        )
    )
    try:
        process = await asyncio.shield(spawning)
    except asyncio.CancelledError:
        # A process may exist before asyncio finishes creating its transport.
        # Keep the semaphore until that child is found, killed and reaped.
        async def reap_spawn() -> None:
            try:
                process = await spawning
            except Exception:
                return
            await _stop_process(process)

        await _wait_cleanup(asyncio.create_task(reap_spawn()))
        raise
    try:
        async with asyncio.timeout(PARSER_CHILD_TIMEOUT_SECONDS):
            result = bytearray()
            assert process.stdout is not None
            while chunk := await process.stdout.read(64 * 1024):
                result.extend(chunk)
                if len(result) > MAX_PARSER_RESPONSE_BYTES:
                    raise ValueError("Parser output exceeds size limit")
            await process.wait()
            if process.returncode != 0:
                raise ValueError("Parser exited unsuccessfully")
            return validated_document(json.loads(result), digest)
    except asyncio.CancelledError:
        await _stop(process)
        raise
    except (TimeoutError, ValueError, TypeError, KeyError):
        await _stop(process)
        return DocumentInfo(
            title=Path(filename).stem[:500] or "이름 없는 문서",
            text="",
            doi=None,
            arxiv_id=None,
            sha256=digest,
            extraction_method="filename",
            warning="문서 분석에 실패하거나 시간이 초과되어 파일명으로 기록했습니다.",
        )


async def _extract(request: web.Request) -> web.Response:
    if request.content_type != "application/octet-stream":
        return _json({"error": "Raw document bytes are required."}, 415)
    kind = request.headers.get("X-Document-Kind", "paper")
    if kind not in {"slides", "paper"}:
        return _json({"error": "Invalid document kind."}, 400)
    try:
        filename = _filename(request.headers.get("X-Filename", ""))
    except (ValueError, UnicodeError):
        return _json({"error": "Invalid document filename."}, 400)
    if request.content_length is not None and request.content_length > MAX_PARSER_FILE_BYTES:
        return _json({"error": "Document size limit exceeded."}, 413)
    try:
        async with asyncio.timeout(PARSER_REQUEST_TIMEOUT_SECONDS):
            async with request.app[_SEMAPHORE]:
                digest = sha256()
                size = 0
                # The random temporary path never contains the submitted name.
                # The parent container only mounts tmpfs, never NAS archives.
                with tempfile.NamedTemporaryFile(
                    mode="wb", prefix="seminar-parser-", suffix=".upload", dir="/tmp"
                ) as saved:
                    async for chunk in request.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > MAX_PARSER_FILE_BYTES:
                            return _json({"error": "Document size limit exceeded."}, 413)
                        saved.write(chunk)
                        digest.update(chunk)
                    if size == 0:
                        return _json({"error": "Document is empty."}, 400)
                    saved.flush()
                    result = await _extract_saved_isolated(
                        Path(saved.name), filename, digest.hexdigest(), kind=kind
                    )
                return _json(asdict(result))
    except Exception:
        # No exception strings, paths, filenames, or environment values cross
        # this boundary. Cancellation still propagates to stop the subprocess.
        return _json({"error": "Document parser is temporarily unavailable."}, 503)


def create_parser_app() -> web.Application:
    app = web.Application(client_max_size=MAX_PARSER_FILE_BYTES)
    app[_SEMAPHORE] = asyncio.Semaphore(MAX_CONCURRENT_PARSERS)
    app.router.add_post("/extract", _extract)
    return app


def main() -> None:
    web.run_app(create_parser_app(), host="0.0.0.0", port=8081, access_log=None, print=None)


if __name__ == "__main__":
    main()

"""Fail-closed client for the parser on the isolated Docker network."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp

from .documents import DocumentInfo

MAX_PARSER_RESPONSE_BYTES = 512 * 1024
PARSER_TIMEOUT_SECONDS = 35
_METHODS = {"filename", "pdf_metadata", "pdf_text", "pptx_metadata", "pptx_text"}


class ParserUnavailable(RuntimeError):
    """The isolated parser is unavailable or returned an invalid document."""

    def __init__(
        self,
        message: str,
        code: str = "parser_unavailable",
        upstream_status: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.upstream_status = upstream_status


def validated_document(payload: object, expected_sha256: str) -> DocumentInfo:
    """Treat parser output as untrusted, including its content checksum."""
    if not isinstance(payload, dict) or set(payload) != {
        "title",
        "text",
        "doi",
        "arxiv_id",
        "sha256",
        "extraction_method",
        "warning",
    }:
        raise ValueError("Invalid document result")
    for key, maximum in {"title": 512, "text": 50_000}.items():
        if not isinstance(payload[key], str) or len(payload[key]) > maximum:
            raise ValueError("Invalid document text")
    if not payload["title"].strip():
        raise ValueError("Missing document title")
    for key, maximum in {"doi": 255, "arxiv_id": 128, "warning": 1024}.items():
        value = payload[key]
        if value is not None and (not isinstance(value, str) or len(value) > maximum):
            raise ValueError("Invalid document metadata")
    if payload["sha256"] != expected_sha256 or payload["extraction_method"] not in _METHODS:
        raise ValueError("Invalid document identity")
    return DocumentInfo(**payload)


def _parser_endpoint(parser_url: str) -> str:
    try:
        url = urlsplit(parser_url)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in {"", "/"}
            or url.query
            or url.fragment
        ):
            raise ValueError
        _ = url.port  # Reject malformed port numbers before making a request.
        return f"{url.scheme}://{url.netloc}/extract"
    except (TypeError, ValueError, AttributeError):
        raise ParserUnavailable(
            "문서 분석 서버 연결 설정이 올바르지 않습니다.", "parser_config_invalid"
        ) from None


async def extract_remote(
    path: Path, filename: str, data: bytes, parser_url: str, *, kind: str = "paper"
) -> DocumentInfo:
    """Send bytes to the isolated parser, never falling back to local parsing.

    ``path`` matches the existing archive helper's interface. No local path,
    Discord token, NAS API token, or environment credentials are transmitted.
    """
    if kind not in {"slides", "paper"}:
        raise ValueError("Invalid document kind")
    endpoint = _parser_endpoint(parser_url)
    expected = hashlib.sha256(data).hexdigest()
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=PARSER_TIMEOUT_SECONDS),
            trust_env=False,
            auto_decompress=False,
        ) as session:
            async with session.post(
                endpoint,
                data=data,
                headers={
                    "Content-Type": "application/octet-stream",
                    "X-Filename": quote(filename, safe=""),
                    "X-Document-Kind": kind,
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                },
                allow_redirects=False,
            ) as response:
                if response.status != 200:
                    raise ParserUnavailable(
                        "문서 분석 서버가 요청을 처리하지 못했습니다.",
                        "parser_rejected",
                        response.status,
                    )
                if response.content_type != "application/json":
                    raise ValueError("Invalid parser response type")
                if (
                    response.headers.get("Content-Encoding", "identity") != "identity"
                    or response.content_length is not None
                    and response.content_length > MAX_PARSER_RESPONSE_BYTES
                ):
                    raise ValueError("Invalid parser response size")
                result = bytearray()
                async for chunk in response.content.iter_chunked(64 * 1024):
                    result.extend(chunk)
                    if len(result) > MAX_PARSER_RESPONSE_BYTES:
                        raise ValueError("Parser response exceeds size limit")
                return validated_document(json.loads(result), expected)
    except TimeoutError:
        raise ParserUnavailable(
            "문서 분석 서버의 응답 시간이 초과되었습니다.", "parser_timeout"
        ) from None
    except aiohttp.ClientError:
        raise ParserUnavailable(
            "문서 분석 서버를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요."
        ) from None
    except (ValueError, TypeError, KeyError):
        raise ParserUnavailable(
            "문서 분석 서버의 응답 형식이 올바르지 않습니다.", "parser_invalid_response"
        ) from None

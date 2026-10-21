"""Extract searchable paper records without executing uploaded documents.

Only a small prefix of a document is inspected. Images are never rendered and
there is deliberately no network lookup or OCR step.
"""

from __future__ import annotations

import hashlib
import io
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from pypdf import PdfReader

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARACTERS = 50_000
MAX_PDF_PAGES = 3
MAX_PDF_CONTENT_BYTES = 8 * 1024 * 1024
MAX_SLIDES = 5
MAX_ZIP_MEMBERS = 2_000
MAX_ZIP_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_XML_BYTES = 2 * 1024 * 1024
MAX_SELECTED_XML_BYTES = 12 * 1024 * 1024

_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOI = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_ARXIV = re.compile(
    r"(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/|10\.48550/arxiv\.)"
    r"(?P<id>(?:\d{4}\.\d{4,5}|[a-z][a-z.\-]+/\d{7}))(?:v\d+)?",
    re.IGNORECASE,
)
_REFERENCES = re.compile(
    r"^\s*(?:\d+[.\s]+)?(?:references|bibliography|참고\s*문헌)\s*[:：]?\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_GENERIC_TITLES = {
    "untitled",
    "untitled presentation",
    "document",
    "presentation",
    "paper",
    "microsoft powerpoint",
    "microsoft word",
    "powerpoint presentation",
    "슬라이드 제목",
    "제목 없음",
    "powerpoint 프레젠테이션",
}


@dataclass(frozen=True)
class DocumentInfo:
    title: str
    text: str
    doi: str | None
    arxiv_id: str | None
    sha256: str
    extraction_method: str
    warning: str | None = None


def validate_paper_fields(payload: dict, *, slides: bool = False) -> dict:
    """Validate user-entered metadata without fetching any DOI or URL."""
    allowed = {"title"} if slides else {"title", "venue", "year", "doi", "link"}
    if not isinstance(payload, dict) or not set(payload) <= allowed or "title" not in payload:
        raise ValueError("논문 제목과 올바른 정보 항목을 입력해주세요.")

    def text(name: str, limit: int, required: bool = False) -> str | None:
        value = payload.get(name)
        if value is None and not required:
            return None
        if not isinstance(value, str) or any(
            ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value
        ):
            raise ValueError("정보에 제어 문자 또는 잘못된 값을 사용할 수 없습니다.")
        value = " ".join(value.split())
        if len(value) > limit or required and not value:
            raise ValueError(f"{name} 항목의 길이를 확인해주세요.")
        return value or None

    result = {"title": text("title", 500, True)}
    if slides:
        return result
    # PATCH is a full metadata form: omitted optional fields are cleared.
    result["venue"] = text("venue", 200)
    year = payload.get("year")
    if year in (None, ""):
        year = None
    elif type(year) is not int or not 1000 <= year <= datetime.now(UTC).year + 2:
        raise ValueError("발표 연도는 1000년부터 현재 연도 + 2년 사이의 정수로 입력해주세요.")
    result["year"] = year
    doi = text("doi", 2048)
    if doi:
        doi = unquote(
            re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", doi, flags=re.I)
        ).casefold()
        if (
            len(doi) > 255
            or not re.fullmatch(r"10\.[0-9]{4,9}/[^\s<>\"\\]+", doi)
            or any(ord(char) < 32 or ord(char) == 127 for char in doi)
        ):
            raise ValueError("DOI 형식을 확인해주세요. 예: 10.1234/paper")
    result["doi"] = doi
    link = text("link", 2048)
    if link:
        try:
            parsed = urlsplit(link)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("논문 링크를 확인해주세요.") from exc
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or port not in {None, 443}
            or any(char.isspace() for char in link)
        ):
            raise ValueError("논문 링크는 계정 정보가 없는 HTTPS 주소로 입력해주세요.")
    result["link"] = link
    return result


class _ExtractionLimit(ValueError):
    """The uploaded document exceeds an extraction resource limit."""


def normalize_search(text: str) -> str:
    """Make substring searches insensitive to case, width, and separators."""
    return "".join(
        character
        for character in unicodedata.normalize("NFKC", text).casefold()
        if character.isalnum()
    )


def normalize_title(text: str) -> str:
    return normalize_search(text)


def _clean_title(title: str) -> str:
    # Drop control characters without throwing away Korean or other Unicode.
    return " ".join(
        "".join(
            c for c in unicodedata.normalize("NFKC", title) if c.isprintable() or c.isspace()
        ).split()
    )[:512]


def _useful_title(title: str | None) -> str | None:
    if not title:
        return None
    candidate = _clean_title(title)
    lowered = candidate.casefold()
    if (
        len(candidate) < 3
        or sum(c.isalpha() for c in candidate) < 3
        or lowered in _GENERIC_TITLES
        or re.search(r"\.(?:docx?|pptx?|pdf)$", lowered)
        or lowered.startswith(("microsoft word -", "microsoft powerpoint -"))
    ):
        return None
    return candidate


def _filename_title(filename: str) -> str:
    stem = PurePosixPath(filename.replace("\\", "/")).stem
    return _clean_title(stem.replace("_", " ")) or "이름 없는 문서"


def _title_from_text(text: str) -> str | None:
    for line in text.splitlines()[:30]:
        candidate = _useful_title(line)
        if candidate is None:
            continue
        lowered = candidate.casefold()
        if (
            lowered.startswith(
                (
                    "arxiv",
                    "doi:",
                    "https://",
                    "http://",
                    "www.",
                    "published as",
                    "proceedings of",
                    "accepted at",
                    "copyright",
                )
            )
            or lowered in {"abstract", "introduction", "초록", "서론", "references"}
            or "@" in candidate
            or _DOI.fullmatch(candidate)
        ):
            continue
        return candidate
    return None


def _identifiers(text: str) -> tuple[str | None, str | None]:
    # A references slide/page must not turn an unrelated citation into the
    # uploaded paper's identity. Ambiguous identifiers are left unset.
    match = _REFERENCES.search(text)
    if match:
        text = text[: match.start()]
    dois: set[str] = set()
    for match in _DOI.finditer(text):
        candidate = match.group().rstrip(".,;:").casefold()
        while candidate.endswith(")") and candidate.count(")") > candidate.count("("):
            candidate = candidate[:-1]
        if len(candidate) <= 255:
            dois.add(candidate)
    arxiv_ids: set[str] = set()
    for match in _ARXIV.finditer(text):
        candidate = match.group("id").casefold()
        if candidate[0].isdigit() and not 1 <= int(candidate[2:4]) <= 12:
            continue
        arxiv_ids.add(candidate)
    return (
        next(iter(dois)) if len(dois) == 1 else None,
        next(iter(arxiv_ids)) if len(arxiv_ids) == 1 else None,
    )


def _pdf(data: bytes) -> tuple[str | None, list[str], str | None]:
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        return None, [], "암호화된 PDF는 파일명으로 기록했습니다."
    metadata_title = _useful_title(reader.metadata.title if reader.metadata else None)
    pages: list[str] = []
    expanded_bytes = 0
    for page in reader.pages[:MAX_PDF_PAGES]:
        contents = page.get_contents()
        if contents is not None:
            expanded_bytes += len(contents.get_data())
            if expanded_bytes > MAX_PDF_CONTENT_BYTES:
                raise _ExtractionLimit("PDF expanded content limit exceeded")
        pages.append((page.extract_text() or "")[:MAX_TEXT_CHARACTERS])
    return metadata_title, pages, None


def _pptx(data: bytes) -> tuple[str | None, list[str], str | None]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) > MAX_ZIP_MEMBERS:
            raise _ExtractionLimit("Too many ZIP members")
        if len({member.filename for member in members}) != len(members):
            raise ValueError("Duplicate ZIP members")
        if sum(member.file_size for member in members) > MAX_ZIP_EXPANDED_BYTES:
            raise _ExtractionLimit("ZIP expanded size limit exceeded")
        member_map = {member.filename: member for member in members}
        selected_bytes = 0

        def read_xml(name: str) -> ElementTree.Element:
            nonlocal selected_bytes
            member = member_map[name]
            selected_bytes += member.file_size
            if member.file_size > MAX_XML_BYTES or selected_bytes > MAX_SELECTED_XML_BYTES:
                raise _ExtractionLimit("XML expanded size limit exceeded")
            with archive.open(member) as stream:
                xml = stream.read(MAX_XML_BYTES + 1)
            if len(xml) > MAX_XML_BYTES:
                raise _ExtractionLimit("XML read limit exceeded")
            # PPTX needs no DTDs. Null removal also catches UTF-16 declarations.
            upper = xml.replace(b"\x00", b"").upper()
            if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
                raise ValueError("XML entity declarations are not supported")
            return ElementTree.fromstring(xml)

        metadata_title: str | None = None
        if "docProps/core.xml" in member_map:
            metadata = read_xml("docProps/core.xml")
            metadata_title = _useful_title(
                metadata.findtext("{http://purl.org/dc/elements/1.1/}title")
            )

        # Follow presentation relationships: slide file numbers do not
        # necessarily match their visible order after a deck is rearranged.
        slide_names: list[str] = []
        if "ppt/presentation.xml" in member_map and "ppt/_rels/presentation.xml.rels" in member_map:
            presentation = read_xml("ppt/presentation.xml")
            relationships = read_xml("ppt/_rels/presentation.xml.rels")
            targets: dict[str, str] = {}
            for relation in relationships.findall(f"{{{_PACKAGE_REL_NS}}}Relationship"):
                if relation.get("TargetMode") == "External":
                    continue
                target = relation.get("Target", "")
                # No archive extraction or outside paths are needed.
                if target.startswith("slides/") and ".." not in PurePosixPath(target).parts:
                    targets[relation.get("Id", "")] = "ppt/" + target
            for slide in presentation.findall(f".//{{{_PRESENTATION_NS}}}sldId")[:MAX_SLIDES]:
                name = targets.get(slide.get(f"{{{_REL_NS}}}id", ""))
                if name and name in member_map:
                    slide_names.append(name)
        else:
            # Also tolerate minimal/generated decks without order metadata.
            slide_names = sorted(
                (name for name in member_map if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)),
                key=lambda name: int(re.search(r"slide(\d+)\.xml$", name).group(1)),
            )[:MAX_SLIDES]

        pages: list[str] = []
        for name in slide_names:
            slide = read_xml(name)
            paragraphs = []
            for paragraph in slide.iter(f"{{{_DRAWING_NS}}}p"):
                paragraphs.append(
                    "".join(node.text or "" for node in paragraph.iter(f"{{{_DRAWING_NS}}}t"))
                )
            pages.append("\n".join(paragraphs)[:MAX_TEXT_CHARACTERS])
        return metadata_title, pages, None


def extract_document(data: bytes, filename: str) -> DocumentInfo:
    """Read PDF/PPTX text; retain legacy PPT uploads as filename records."""
    extension = PurePosixPath(filename.replace("\\", "/")).suffix.casefold()
    if extension not in {".pdf", ".ppt", ".pptx"}:
        raise ValueError("PDF, PPT, PPTX 파일만 등록할 수 있습니다.")
    digest = hashlib.sha256(data).hexdigest()
    fallback = _filename_title(filename)

    def filename_record(warning: str) -> DocumentInfo:
        return DocumentInfo(fallback, "", None, None, digest, "filename", warning)

    if len(data) > MAX_FILE_BYTES:
        return filename_record("파일 크기 제한을 넘어 파일명으로 기록했습니다.")
    if extension == ".ppt":
        return filename_record(
            ".ppt 원본을 보관했습니다. 본문 분석은 PDF/PPTX 형식에서 지원합니다."
        )
    try:
        metadata_title, pages, warning = _pdf(data) if extension == ".pdf" else _pptx(data)
        text = "\n\n".join(pages)[:MAX_TEXT_CHARACTERS].strip()
        if not text:
            if metadata_title:
                return DocumentInfo(
                    metadata_title,
                    "",
                    None,
                    None,
                    digest,
                    f"{extension[1:]}_metadata",
                    warning
                    or "본문 텍스트를 찾지 못해 문서 제목으로 기록했습니다. 스캔 PDF에는 OCR이 필요합니다.",
                )
            return filename_record(
                warning
                or "본문 텍스트를 찾지 못해 파일명으로 기록했습니다. 스캔 PDF에는 OCR이 필요합니다."
            )
        title = metadata_title or _title_from_text(pages[0] if pages else "") or fallback
        method = f"{extension[1:]}_metadata" if metadata_title else f"{extension[1:]}_text"
        if title == fallback and not metadata_title:
            method = "filename"
        doi, arxiv_id = _identifiers(text)
        return DocumentInfo(title, text, doi, arxiv_id, digest, method, warning)
    except _ExtractionLimit:
        return filename_record("문서 분석 용량 제한을 넘어 파일명으로 기록했습니다.")
    except Exception:
        # A damaged file must not make its Discord upload disappear from the
        # archive. It remains searchable by filename and checksum.
        return filename_record("파일을 해석하지 못해 파일명으로 기록했습니다.")

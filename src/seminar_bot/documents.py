"""Extract searchable paper records without executing uploaded documents.

Only a small prefix of a document is inspected. Images are never rendered and
there is deliberately no network lookup or OCR step.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from pypdf import PdfReader, mult

MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TEXT_CHARACTERS = 50_000
MAX_PDF_PAGES = 3
MAX_PDF_CONTENT_BYTES = 8 * 1024 * 1024
MAX_SLIDES = 5
MAX_ZIP_MEMBERS = 2_000
MAX_ZIP_EXPANDED_BYTES = 100 * 1024 * 1024
MAX_XML_BYTES = 2 * 1024 * 1024
MAX_SELECTED_XML_BYTES = 12 * 1024 * 1024
MAX_VISUAL_FRAGMENTS = 2_000
MAX_VISUAL_LINES = 600

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


_PAPER_AFFILIATION = re.compile(
    r"\b(?:university|universit[eé]|institute|institution|department|faculty|"
    r"college|laboratory|academy|school of|research (?:council|center|centre|lab))\b"
    r"|대학교|대학원|연구소",
    re.IGNORECASE,
)
_PAPER_SECTION = re.compile(
    r"^(?:abstract|초록)\s*(?:$|[:：—–-])"
    r"|^(?:(?:\d+|[ivx]+)[.\s]+)?(?:introduction|summary|references|bibliography|서론|참고\s*문헌)\s*[:：]?\s*$"
    r"|^(?:keywords?|index terms)\s*(?:$|[:：—–-])",
    re.IGNORECASE,
)
_TITLE_CONNECTORS = {
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "for",
    "from",
    "with",
    "without",
    "by",
    "via",
    "toward",
    "towards",
    "through",
    "using",
    "based",
    "under",
}


def _paper_boilerplate(line: str) -> bool:
    return bool(
        re.search(
            r"^(?:this (?:paper|article) is (?:included|published)|open access to "
            r"|published in |proceedings of |copyright\b|©|isbn\b|issn\b)"
            r"|^\d+(?:st|nd|rd|th)\b.*\b(?:symposium|conference|workshop|congress)\b"
            r"|\b(?:symposium|conference|workshop)\b.*(?:sponsored by|\b(?:19|20)\d{2}\b)"
            r"|^(?:january|february|march|april|may|june|july|august|september|october|november|december)"
            r"\b.*\b(?:19|20)\d{2}\b",
            line,
            re.IGNORECASE,
        )
    )


def _paper_author_line(line: str, following: str = "", *, allow_single_name: bool = True) -> bool:
    if (
        "@" in line
        or _PAPER_AFFILIATION.search(line)
        or re.match(r"^(?:(?:an?\s+)?authors?\b|presenter\b|by\s+)", line, re.IGNORECASE)
    ):
        return True

    def person_name(value: str) -> bool:
        value = re.sub(r"[\d*∗†‡§]+", "", value).strip()
        words = value.split()
        return (
            2 <= len(words) <= 4
            and all(
                word[0].isupper() and word.casefold() not in _TITLE_CONNECTORS for word in words
            )
            and all(re.fullmatch(r"[^\W\d_][\w.’'\-]*", word) for word in words)
        )

    names = [name.strip() for name in re.split(r"[,;]|\s+(?:and|&)\s+", line) if name.strip()]
    if not names or not all(person_name(name) for name in names):
        return False
    if len(names) == 1 and not allow_single_name:
        return False
    # Commas alone are insufficient: title continuations can also contain
    # comma-separated phrases. Author markers or the next section help bound it.
    return (
        len(names) >= 3
        or bool(re.search(r"[∗†‡§]|\b(?:and|&)\b", line))
        or bool(_PAPER_AFFILIATION.search(following))
        or "@" in following
        or bool(_PAPER_SECTION.match(following))
    )


def _paper_title_from_text(text: str) -> str | None:
    """Restore a paper's textual heading without inspecting font sizes."""
    lines = [_clean_title(line) for line in text.splitlines()[:40]]
    for start, line in enumerate(lines):
        if not line or _paper_boilerplate(line):
            continue
        if _PAPER_SECTION.match(line):
            return None
        first = _title_from_text(line)
        if not first:
            continue
        parts = [first]
        for index in range(start + 1, min(start + 5, len(lines))):
            continuation = lines[index]
            following = lines[index + 1] if index + 1 < len(lines) else ""
            unfinished = (
                parts[-1].endswith((":", "-", "\u00ad"))
                or parts[-1].split()[-1].casefold() in _TITLE_CONNECTORS
            )
            if (
                not continuation
                or _paper_boilerplate(continuation)
                or _PAPER_SECTION.match(continuation)
                or _paper_author_line(
                    continuation,
                    following,
                    allow_single_name=not unfinished or len(continuation.split()) <= 3,
                )
                or not _title_from_text(continuation)
                or re.search(r"[.!?。]$", parts[-1])
            ):
                break
            words = re.findall(r"[^\W\d_][\w'’\-]*", continuation)
            significant = [word for word in words if word.casefold() not in _TITLE_CONNECTORS]
            title_case = (
                significant
                and sum(word[0].isupper() for word in significant) >= len(significant) * 0.6
            )
            if not title_case and not unfinished:
                break
            if parts[-1].endswith(("-", "\u00ad")):
                parts[-1] = parts[-1].removesuffix("\u00ad") + continuation
            else:
                parts.append(continuation)
        return _useful_title(" ".join(parts))
    return None


@dataclass(frozen=True)
class _VisualLine:
    text: str
    size: float
    x: float
    y: float  # Distance from the top, in points.


def _visual_text(text: str) -> str | None:
    candidate = _useful_title(text)
    if not candidate or len(candidate) > 240 or _title_from_text(candidate) != candidate:
        return None
    letters = "".join(character for character in candidate if character.isalpha())
    # A giant logo/acronym must not outrank a substantive title beneath it.
    if len(letters) <= 5 and letters.isascii() and letters.isupper():
        return None
    return candidate


def _largest_title(
    lines: list[_VisualLine], width: float, height: float, *, minimum_size: float = 0
) -> str | None:
    """Pick a prominent, coherent text block, rather than a single large glyph."""
    minimum_size = max(minimum_size, 12.0, min(width, height) * 0.025)
    candidates = [
        line
        for line in lines[:MAX_VISUAL_LINES]
        if (
            minimum_size <= line.size <= min(144.0, height * 0.25)
            and 0.04 * height <= line.y <= 0.9 * height
            and -0.02 * width <= line.x <= 1.02 * width
            and _visual_text(line.text)
        )
    ]
    if not candidates:
        return None
    largest = max(line.size for line in candidates)
    prominent = sorted(
        (line for line in candidates if line.size >= largest * 0.8),
        key=lambda line: (line.y, line.x),
    )
    blocks: list[list[_VisualLine]] = []
    for line in prominent:
        if blocks:
            previous = blocks[-1][-1]
            if (
                len(blocks[-1]) < 3
                and 0 < line.y - previous.y <= max(line.size, previous.size) * 1.8
                and abs(line.x - previous.x) <= width * 0.22
            ):
                blocks[-1].append(line)
                continue
        blocks.append([line])
    block = max(
        blocks,
        key=lambda group: (
            max(line.size for line in group),
            min(sum(len(normalize_search(line.text)) for line in group), 120),
            -group[0].y,
        ),
    )
    return _useful_title(" ".join(line.text for line in block))


def _pdf_visual_title(fragments: list[_VisualLine], width: float, height: float) -> str | None:
    # PDF fragments may split one visual line into several font runs. Merge
    # equal-baseline runs before rejecting short logo-like text fragments.
    rows: list[list[_VisualLine]] = []
    for fragment in sorted(fragments, key=lambda line: (line.y, line.x)):
        if rows:
            previous = rows[-1][-1]
            if (
                abs(fragment.y - previous.y) <= max(2, min(fragment.size, previous.size) * 0.2)
                and max(fragment.size, previous.size) <= min(fragment.size, previous.size) * 1.35
            ):
                rows[-1].append(fragment)
                continue
        if len(rows) >= MAX_VISUAL_LINES:
            break
        rows.append([fragment])
    lines = []
    for row in rows:
        row.sort(key=lambda fragment: fragment.x)
        text = _clean_title(" ".join(fragment.text for fragment in row))
        sizes = [(fragment.size, max(1, len(normalize_search(fragment.text)))) for fragment in row]
        lines.append(_VisualLine(text, _weighted_size(sizes), row[0].x, row[0].y))
    return _largest_title(lines, width, height)


def _weighted_size(sizes: list[tuple[float, int]]) -> float:
    """Character-weighted median keeps an oversized initial from dominating."""
    sizes = sorted((size, count) for size, count in sizes if 0 < size <= 144)
    halfway = sum(count for _, count in sizes) / 2
    cumulative = 0
    for size, count in sizes:
        cumulative += count
        if cumulative >= halfway:
            return size
    return 0


def _pptx_visual_title(slide: ElementTree.Element, width: float, height: float) -> str | None:
    lines: list[_VisualLine] = []
    title_shapes: list[tuple[str, float]] = []

    def font_size(properties: ElementTree.Element | None) -> float:
        try:
            return float(properties.get("sz", "0")) / 100 if properties is not None else 0
        except (TypeError, ValueError, OverflowError):
            return 0

    for shape_number, shape in enumerate(slide.iter(f"{{{_PRESENTATION_NS}}}sp")):
        if shape_number >= MAX_VISUAL_LINES:
            break
        nonvisual = shape.find(f"{{{_PRESENTATION_NS}}}nvSpPr/{{{_PRESENTATION_NS}}}cNvPr")
        if nonvisual is not None and nonvisual.get("hidden") in {"1", "true"}:
            continue
        body = shape.find(f"{{{_PRESENTATION_NS}}}txBody")
        if body is None:
            continue
        placeholder = shape.find(f".//{{{_PRESENTATION_NS}}}ph")
        is_title = placeholder is not None and placeholder.get("type") in {"title", "ctrTitle"}
        offset = shape.find(
            f"{{{_PRESENTATION_NS}}}spPr/{{{_DRAWING_NS}}}xfrm/{{{_DRAWING_NS}}}off"
        )
        try:
            x = float(offset.get("x", "0")) / 12700 if offset is not None else 0
            y = float(offset.get("y", "0")) / 12700 if offset is not None else height * 0.1
        except (ValueError, OverflowError):
            continue
        if not all(math.isfinite(value) for value in (x, y)):
            continue
        shape_default = font_size(
            body.find(f"{{{_DRAWING_NS}}}lstStyle/{{{_DRAWING_NS}}}lvl1pPr/{{{_DRAWING_NS}}}defRPr")
        )
        shape_text: list[str] = []
        shape_sizes: list[tuple[float, int]] = []
        for paragraph in body.findall(f"{{{_DRAWING_NS}}}p")[:MAX_VISUAL_LINES]:
            default = (
                font_size(paragraph.find(f"{{{_DRAWING_NS}}}pPr/{{{_DRAWING_NS}}}defRPr"))
                or shape_default
            )
            parts: list[str] = []
            sizes: list[tuple[float, int]] = []
            for run in list(paragraph)[:MAX_VISUAL_FRAGMENTS]:
                if run.tag == f"{{{_DRAWING_NS}}}br":
                    parts.append(" ")
                    continue
                text = "".join(node.text or "" for node in run.iter(f"{{{_DRAWING_NS}}}t"))
                if text:
                    parts.append(text)
                    size = font_size(run.find(f"{{{_DRAWING_NS}}}rPr")) or default
                    sizes.append((size, max(1, len(normalize_search(text)))))
            text = _clean_title("".join(parts))
            size = _weighted_size(sizes)
            if text:
                shape_text.append(text)
                shape_sizes.extend(sizes)
                if len(lines) < MAX_VISUAL_LINES:
                    lines.append(_VisualLine(text, size, x, y))
                y += max(size, 12) * 1.2
        title = _visual_text(" ".join(shape_text))
        if is_title and title:
            title_shapes.append((title, _weighted_size(shape_sizes)))
    # Known font sizes remain the primary visual evidence. An explicitly small
    # placeholder may just be the deck's recurring seminar label. If its size
    # is inherited/unknown, the placeholder is still a useful cover-title hint.
    if title_shapes:
        title, size = max(title_shapes, key=lambda candidate: (candidate[1], len(candidate[0])))
        if size:
            larger = _largest_title(lines, width, height, minimum_size=size + 0.5)
            if larger:
                return larger
        return title
    return _largest_title(lines, width, height)


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


def _pdf(
    data: bytes, *, visual: bool = False
) -> tuple[str | None, list[str], str | None, str | None]:
    reader = PdfReader(io.BytesIO(data), strict=False)
    if reader.is_encrypted:
        return None, [], "암호화된 PDF는 파일명으로 기록했습니다.", None
    metadata_title = _useful_title(reader.metadata.title if reader.metadata else None)
    pages: list[str] = []
    expanded_bytes = 0
    visual_title = None
    for page_number, page in enumerate(reader.pages[:MAX_PDF_PAGES]):
        contents = page.get_contents()
        if contents is not None:
            expanded_bytes += len(contents.get_data())
            if expanded_bytes > MAX_PDF_CONTENT_BYTES:
                raise _ExtractionLimit("PDF expanded content limit exceeded")
        if visual and page_number == 0:
            fragments: list[_VisualLine] = []
            collected_characters = 0
            width, height = float(page.mediabox.width), float(page.mediabox.height)

            def collect(text, cm, tm, font, font_size, *, fragments=fragments, page=page):
                nonlocal collected_characters
                if (
                    len(fragments) >= MAX_VISUAL_FRAGMENTS
                    or collected_characters >= MAX_TEXT_CHARACTERS
                ):
                    return
                try:
                    matrix = mult(tm, cm)
                    size = abs(float(font_size)) * math.hypot(matrix[2], matrix[3])
                    x = matrix[4] - float(page.mediabox.left)
                    y = float(page.mediabox.top) - matrix[5]
                    if not all(math.isfinite(value) for value in (size, x, y)) or size <= 0:
                        return
                    for offset, line in enumerate(text.splitlines()):
                        if len(fragments) >= MAX_VISUAL_FRAGMENTS:
                            break
                        line = line[: MAX_TEXT_CHARACTERS - collected_characters]
                        collected_characters += len(line)
                        if line.strip():
                            fragments.append(
                                _VisualLine(line.strip(), size, x, y + offset * size * 1.2)
                            )
                except (TypeError, ValueError, OverflowError, IndexError):
                    return

            pages.append((page.extract_text(visitor_text=collect) or "")[:MAX_TEXT_CHARACTERS])
            visual_title = _pdf_visual_title(fragments, width, height)
        else:
            pages.append((page.extract_text() or "")[:MAX_TEXT_CHARACTERS])
    return metadata_title, pages, None, visual_title


def _pptx(
    data: bytes, *, visual: bool = False
) -> tuple[str | None, list[str], str | None, str | None]:
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
        width, height = 720.0, 540.0
        if "ppt/presentation.xml" in member_map and "ppt/_rels/presentation.xml.rels" in member_map:
            presentation = read_xml("ppt/presentation.xml")
            slide_size = presentation.find(f"{{{_PRESENTATION_NS}}}sldSz")
            if slide_size is not None:
                try:
                    dimensions = tuple(
                        float(slide_size.get(name, "0")) / 12700 for name in ("cx", "cy")
                    )
                    if all(math.isfinite(value) and 1 <= value <= 10000 for value in dimensions):
                        width, height = dimensions
                except (ValueError, OverflowError):
                    pass
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
        visual_title = None
        for slide_number, name in enumerate(slide_names):
            slide = read_xml(name)
            if visual and slide_number == 0:
                visual_title = _pptx_visual_title(slide, width, height)
            paragraphs = []
            for paragraph in slide.iter(f"{{{_DRAWING_NS}}}p"):
                paragraphs.append(
                    "".join(node.text or "" for node in paragraph.iter(f"{{{_DRAWING_NS}}}t"))
                )
            pages.append("\n".join(paragraphs)[:MAX_TEXT_CHARACTERS])
        return metadata_title, pages, None, visual_title


def extract_document(data: bytes, filename: str, *, kind: str = "paper") -> DocumentInfo:
    """Read PDF/PPTX text; retain legacy PPT uploads as filename records."""
    if kind not in {"paper", "slides"}:
        raise ValueError("문서 종류는 paper 또는 slides여야 합니다.")
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
        metadata_title, pages, warning, visual_title = (
            _pdf(data, visual=kind == "slides")
            if extension == ".pdf"
            else _pptx(data, visual=kind == "slides")
        )
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
        text_title = _title_from_text(pages[0] if pages else "")
        if kind == "paper" and extension == ".pdf":
            text_title = _paper_title_from_text(pages[0] if pages else "")
            if metadata_title and _paper_boilerplate(metadata_title):
                metadata_title = None
            if metadata_title and text_title:
                metadata_key, text_key = (
                    normalize_title(metadata_title),
                    normalize_title(text_title),
                )
                if (
                    len(metadata_key) >= 12
                    and len(text_key) > len(metadata_key) + 5
                    and text_key.startswith(metadata_key)
                ):
                    # Some PDFs store only the first title line in metadata.
                    metadata_title = None
        title = visual_title or metadata_title or text_title or fallback
        method = (
            f"{extension[1:]}_metadata"
            if metadata_title and not visual_title
            else f"{extension[1:]}_text"
        )
        if title == fallback and not metadata_title and not visual_title:
            method = "filename"
        doi, arxiv_id = _identifiers(text)
        return DocumentInfo(title, text, doi, arxiv_id, digest, method, warning)
    except _ExtractionLimit:
        return filename_record("문서 분석 용량 제한을 넘어 파일명으로 기록했습니다.")
    except Exception:
        # A damaged file must not make its Discord upload disappear from the
        # archive. It remains searchable by filename and checksum.
        return filename_record("파일을 해석하지 못해 파일명으로 기록했습니다.")

"""Conservative, local classification and progress text for seminar uploads."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

from .documents import DocumentInfo, normalize_title

MIN_PAPERS = 3

_SLIDES_FILENAME = re.compile(
    r"(?<![a-z])(?:slides?|presentation)(?![a-z])|발표[\s_.-]*자료|슬라이드",
    re.IGNORECASE,
)
_PAPER_FILENAME = re.compile(r"(?<![a-z])papers?(?![a-z])|논문", re.IGNORECASE)
_GENERIC_TITLES = {
    "untitled",
    "untitledpresentation",
    "document",
    "presentation",
    "paper",
    "microsoftpowerpoint",
    "microsoftword",
    "powerpointpresentation",
    "슬라이드제목",
    "제목없음",
    "powerpoint프레젠테이션",
}


@dataclass(frozen=True)
class Classification:
    kind: Literal["slides", "paper", "unknown"]
    reason: str


def classify_document(
    filename: str, info: DocumentInfo, message_content: str = ""
) -> Classification:
    """Suggest a file's role without treating every PDF as a source paper.

    A message-wide label applies only when the entire message is a short,
    unambiguous label. Filename presentation cues take priority over a DOI or
    a message label: slides frequently contain identifiers for cited papers.
    This is a suggestion; an uploader must be able to correct it later.
    """
    name = unicodedata.normalize("NFKC", filename).replace("\\", "/")
    path = PurePosixPath(name)
    suffix = path.suffix.casefold()
    if suffix in {".ppt", ".pptx"}:
        return Classification("slides", "PowerPoint 발표 자료")
    if suffix != ".pdf":
        return Classification("unknown", "PDF/PPT/PPTX 파일이 아니어서 분류할 수 없습니다.")

    if _SLIDES_FILENAME.search(path.stem):
        return Classification("slides", "파일명의 발표 자료 표시")

    label = " ".join(unicodedata.normalize("NFKC", message_content).strip().split())
    if label in {"발표자료", "발표 자료", "슬라이드"}:
        return Classification("slides", "업로드 메시지의 발표 자료 표시")
    if label == "논문":
        return Classification("paper", "업로드 메시지의 논문 표시")

    if _PAPER_FILENAME.search(path.stem):
        return Classification("paper", "파일명의 논문 표시")
    if info.doi or info.arxiv_id:
        return Classification("paper", "PDF에서 추출한 DOI/arXiv 식별자")
    return Classification("unknown", "발표 자료인지 논문 PDF인지 확인이 필요합니다.")


def _extension(record: dict) -> str:
    name = unicodedata.normalize("NFKC", str(record.get("filename") or ""))
    return PurePosixPath(name.replace("\\", "/")).suffix.casefold()


def _sha(record: dict) -> str:
    return str(record.get("sha256") or "").strip().casefold()


def _paper_identity_keys(record: dict, index: int) -> set[tuple[str, str]]:
    keys = {("row", str(index))}
    if sha := _sha(record):
        keys.add(("sha", sha))
    if doi := str(record.get("doi") or "").strip().casefold():
        doi = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)", "", doi)
        keys.add(("doi", doi))
    if arxiv := str(record.get("arxiv_id") or "").strip().casefold():
        arxiv = re.sub(r"^(?:https?://arxiv\.org/(?:abs|pdf)/|arxiv\s*:\s*)", "", arxiv)
        arxiv = re.sub(r"(?:v\d+)?(?:\.pdf)?$", "", arxiv)
        keys.add(("arxiv", arxiv))
    if record.get("extraction_method") == "pdf_metadata":
        raw_title = str(record.get("title") or "").strip()
        title = normalize_title(raw_title)
        if (
            len(title) >= 12
            and sum(character.isalpha() for character in title) >= 3
            and title not in _GENERIC_TITLES
            and not re.search(r"\.(?:docx?|pptx?|pdf)$", raw_title, re.IGNORECASE)
            and not raw_title.casefold().startswith(("microsoft word -", "microsoft powerpoint -"))
        ):
            keys.add(("title", title))
    return keys


def count_submission(records: list[dict]) -> dict:
    """Count presentation files and distinct source papers for one seminar.

    Paper identifiers form connected groups: e.g. a file with both DOI and
    arXiv ID connects earlier DOI-only and arXiv-only uploads. A presentation
    file cannot also fill a paper slot under another filename.
    """
    slides = [
        (index, record)
        for index, record in enumerate(records)
        if record.get("kind") == "slides" and _extension(record) in {".pdf", ".ppt", ".pptx"}
    ]
    slide_shas = {_sha(record) for _, record in slides if _sha(record)}
    slide_files = {_sha(record) or f"missing-sha:{index}" for index, record in slides}

    parents: list[int] = []
    key_owner: dict[tuple[str, str], int] = {}

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    for index, record in enumerate(records):
        if record.get("kind") != "paper" or _extension(record) != ".pdf":
            continue
        if _sha(record) and _sha(record) in slide_shas:
            continue
        position = len(parents)
        parents.append(position)
        for key in _paper_identity_keys(record, index):
            if key in key_owner:
                parents[find(position)] = find(key_owner[key])
            else:
                key_owner[key] = position
    paper_count = len({find(position) for position in range(len(parents))})
    unknown_files = {
        _sha(record) or f"missing-sha:{index}"
        for index, record in enumerate(records)
        if record.get("kind") == "unknown"
    }
    missing_papers = max(0, MIN_PAPERS - paper_count)
    return {
        "slides": len(slide_files),
        "papers": paper_count,
        "unknown": len(unknown_files),
        "complete": bool(slide_files) and missing_papers == 0,
        "missing_papers": missing_papers,
    }


def submission_summary(status: dict) -> str:
    """Describe progress using counts of distinct files/paper identities.

    Deduplication and linkage to a seminar are the caller's responsibility.
    Completion is derived from the counts rather than a stale cached flag.
    Unknown files do not count toward either requirement.
    """
    slides = max(0, int(status.get("slides", 0)))
    papers = max(0, int(status.get("papers", 0)))
    unknown = max(0, int(status.get("unknown", 0)))
    missing_papers = max(0, MIN_PAPERS - papers)
    complete = slides >= 1 and missing_papers == 0

    lines = [
        f"제출 기준: 발표 자료 1개 이상 + 서로 다른 논문 PDF {MIN_PAPERS}개 이상",
        f"현재: 발표 자료 {slides}개 · 논문 PDF {papers}개",
    ]
    if complete:
        lines.append("제출 완료: 발표 자료와 논문 PDF 제출 기준을 충족했습니다.")
    else:
        missing = []
        if not slides:
            missing.append("발표 자료 1개")
        if missing_papers:
            missing.append(f"서로 다른 논문 PDF {missing_papers}개")
        lines.append("제출 대기: " + ", ".join(missing) + "가 더 필요합니다.")
    if unknown:
        lines.append(f"분류 확인 대기: {unknown}개 (제출 수에 포함되지 않음)")
    return "\n".join(lines)

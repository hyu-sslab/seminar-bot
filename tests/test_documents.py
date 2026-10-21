from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from seminar_bot.archive import extract_saved
from seminar_bot.documents import (
    MAX_XML_BYTES,
    extract_document,
    normalize_search,
    normalize_title,
)


def pdf_fixture(*pages: str, title: str | None = None, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {
                        NameObject("/F1"): DictionaryObject(
                            {
                                NameObject("/Type"): NameObject("/Font"),
                                NameObject("/Subtype"): NameObject("/Type1"),
                                NameObject("/BaseFont"): NameObject("/Helvetica"),
                            }
                        ),
                    }
                ),
            }
        )
        stream = DecodedStreamObject()
        lines = []
        for line in text.splitlines():
            literal = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            lines.append(f"({literal}) Tj T*")
        stream.set_data(
            ("BT /F1 12 Tf 14 TL 50 700 Td " + " ".join(lines) + " ET").encode("latin1")
        )
        page[NameObject("/Contents")] = writer._add_object(stream)
    if title:
        writer.add_metadata({"/Title": title})
    if encrypted:
        writer.encrypt("secret")
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def pptx_fixture(
    *slides: str, title: str | None = None, extras: dict[str, str] | None = None
) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        if title:
            archive.writestr(
                "docProps/core.xml",
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>'
                + escape(title)
                + "</dc:title></cp:coreProperties>",
            )
        for number, text in enumerate(slides, start=1):
            paragraphs = "".join(
                "<a:p><a:r><a:t>" + escape(line) + "</a:t></a:r></a:p>"
                for line in text.splitlines()
            )
            archive.writestr(
                f"ppt/slides/slide{number}.xml",
                '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                + paragraphs
                + "</p:sld>",
            )
        for name, contents in (extras or {}).items():
            archive.writestr(name, contents)
    return result.getvalue()


def styled_pdf_fixture(
    lines: list[tuple[str, int, int, int]],
    *,
    title: str | None = "Old Template Title",
    later_lines: list[tuple[str, int, int, int]] | None = None,
) -> bytes:
    writer = PdfWriter()
    for fragments in [lines, *([later_lines] if later_lines else [])]:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject(
                    {
                        NameObject("/F1"): DictionaryObject(
                            {
                                NameObject("/Type"): NameObject("/Font"),
                                NameObject("/Subtype"): NameObject("/Type1"),
                                NameObject("/BaseFont"): NameObject("/Helvetica"),
                            }
                        )
                    }
                )
            }
        )
        stream = DecodedStreamObject()
        commands = []
        for text, size, x, y in fragments:
            literal = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({literal}) Tj ET")
        stream.set_data("\n".join(commands).encode("latin1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if title:
        writer.add_metadata({"/Title": title})
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def pptx_shape(
    paragraphs: list[tuple[str, int | None]],
    *,
    default_size: int | None = None,
    placeholder: str | None = None,
    y: int = 100,
) -> str:
    ph = f'<p:ph type="{placeholder}"/>' if placeholder else ""
    content = []
    for text, size in paragraphs:
        default = f'<a:pPr><a:defRPr sz="{default_size * 100}"/></a:pPr>' if default_size else ""
        properties = f'<a:rPr sz="{size * 100}"/>' if size else ""
        content.append(f"<a:p>{default}<a:r>{properties}<a:t>{escape(text)}</a:t></a:r></a:p>")
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="1" name="Text"/><p:cNvSpPr>{ph}</p:cNvSpPr></p:nvSpPr>'
        f'<p:spPr><a:xfrm><a:off x="914400" y="{y * 12700}"/></a:xfrm></p:spPr>'
        "<p:txBody><a:bodyPr/><a:lstStyle/>" + "".join(content) + "</p:txBody></p:sp>"
    )


def styled_pptx_fixture(*shapes: str, title: str = "Old Template Title") -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "docProps/core.xml",
            '<properties xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>'
            + escape(title)
            + "</dc:title></properties>",
        )
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree>'
            + "".join(shapes)
            + "</p:spTree></p:cSld></p:sld>",
        )
    return result.getvalue()


class DocumentTests(unittest.TestCase):
    def test_slides_pdf_uses_large_cover_title_before_metadata_and_joins_lines(self):
        data = styled_pdf_fixture(
            [
                ("LAB", 96, 20, 730),
                ("Presenter Name", 12, 80, 520),
                ("Robust Learning", 36, 80, 650),
                ("for Changing Data", 34, 80, 606),
                ("This is a normal body sentence.", 12, 80, 560),
            ]
        )
        document = extract_document(data, "slides.pdf", kind="slides")
        self.assertEqual(document.title, "Robust Learning for Changing Data")
        self.assertEqual(document.extraction_method, "pdf_text")
        self.assertEqual(extract_document(data, "paper.pdf").title, "Old Template Title")
        explicit_paper = extract_document(data, "paper.pdf", kind="paper")
        self.assertEqual(explicit_paper.title, "Old Template Title")
        self.assertEqual(explicit_paper.extraction_method, "pdf_metadata")

    def test_slides_pdf_joins_separate_font_runs_on_one_line(self):
        data = styled_pdf_fixture(
            [
                ("Learning", 36, 60, 650),
                ("Systems", 34, 235, 650),
                ("A Presenter", 12, 80, 520),
            ]
        )
        self.assertEqual(
            extract_document(data, "slides.pdf", kind="slides").title,
            "Learning Systems",
        )

    def test_slides_pdf_only_uses_first_page_for_visual_title(self):
        data = styled_pdf_fixture(
            [("First Cover Title", 28, 80, 650)],
            later_lines=[("Much Larger Later Title", 72, 80, 650)],
        )
        self.assertEqual(
            extract_document(data, "slides.pdf", kind="slides").title,
            "First Cover Title",
        )

    def test_slides_pdf_does_not_promote_body_text_over_metadata(self):
        data = styled_pdf_fixture([("This is a normal body sentence.", 12, 80, 650)])
        document = extract_document(data, "slides.pdf", kind="slides")
        self.assertEqual(document.title, "Old Template Title")
        self.assertEqual(document.extraction_method, "pdf_metadata")

    def test_slides_pdf_text_and_filename_fallbacks_still_work(self):
        document = extract_document(
            pdf_fixture("Useful Cover Title\nPresenter Name"), "slides.pdf", kind="slides"
        )
        self.assertEqual(document.title, "Useful Cover Title")
        blank = extract_document(pdf_fixture(""), "발표_자료.pdf", kind="slides")
        self.assertEqual(blank.title, "발표 자료")
        self.assertTrue(blank.warning)

    def test_slides_pptx_large_runs_override_metadata_and_ignore_large_logo(self):
        data = styled_pptx_fixture(
            pptx_shape([("LAB", 96)], y=35),
            pptx_shape([("발표자 이름", 12)], y=300),
            pptx_shape([("변화하는 데이터에서", 40), ("안정적인 학습 방법", 36)]),
        )
        document = extract_document(data, "slides.pptx", kind="slides")
        self.assertEqual(document.title, "변화하는 데이터에서 안정적인 학습 방법")
        self.assertEqual(document.extraction_method, "pptx_text")
        self.assertEqual(extract_document(data, "paper.pptx").title, "Old Template Title")

    def test_slides_pptx_uses_paragraph_default_size(self):
        data = styled_pptx_fixture(
            pptx_shape([("Small decoration", 14)], y=60),
            pptx_shape([("Default Styled Cover Title", None)], default_size=36),
        )
        self.assertEqual(
            extract_document(data, "slides.pptx", kind="slides").title,
            "Default Styled Cover Title",
        )

    def test_slides_pptx_soft_line_break_does_not_join_title_words(self):
        shape = pptx_shape([("Robust Learning", 36)], placeholder="title")
        shape = shape.replace(
            "</a:r></a:p>",
            '</a:r><a:br/><a:r><a:rPr sz="3600"/><a:t>for Changing Data</a:t></a:r></a:p>',
        )
        self.assertEqual(
            extract_document(styled_pptx_fixture(shape), "slides.pptx", kind="slides").title,
            "Robust Learning for Changing Data",
        )

    def test_slides_pptx_prefers_title_placeholder_without_explicit_size(self):
        for placeholder in ("title", "ctrTitle"):
            with self.subTest(placeholder=placeholder):
                data = styled_pptx_fixture(
                    pptx_shape([("Oversized Decorative Label", 90)], y=30),
                    pptx_shape([("Actual Cover Title", None)], placeholder=placeholder),
                )
                self.assertEqual(
                    extract_document(data, "slides.pptx", kind="slides").title,
                    "Actual Cover Title",
                )

    def test_slides_pptx_small_explicit_placeholder_does_not_override_large_title(self):
        data = styled_pptx_fixture(
            pptx_shape([("Weekly seminar", 12)], placeholder="title", y=35),
            pptx_shape([("Visible Research Title", 36)]),
        )
        self.assertEqual(
            extract_document(data, "slides.pptx", kind="slides").title,
            "Visible Research Title",
        )

    def test_invalid_document_kind_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "paper.*slides"):
            extract_document(b"example", "slides.pdf", kind="unknown")

    def test_pdf_uses_metadata_and_extracts_identifiers(self):
        data = pdf_fixture(
            "Text Title\nhttps://doi.org/10.1234/ABC.123).\narXiv:2401.12345v3",
            title="A Better Metadata Title",
        )
        document = extract_document(data, "paper.PDF")
        self.assertEqual(document.title, "A Better Metadata Title")
        self.assertEqual(document.doi, "10.1234/abc.123")
        self.assertEqual(document.arxiv_id, "2401.12345")
        self.assertEqual(document.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(document.extraction_method, "pdf_metadata")
        self.assertIsNone(document.warning)

    def test_pdf_ignores_generic_metadata_and_uses_first_page(self):
        document = extract_document(
            pdf_fixture("Useful Paper Title\nAn Author\nAbstract", title="Untitled"), "upload.pdf"
        )
        self.assertEqual(document.title, "Useful Paper Title")
        self.assertEqual(document.extraction_method, "pdf_text")

    def test_paper_reconstructs_three_title_lines_without_appending_authors(self):
        data = pdf_fixture(
            "Adapting Policies to Signals: Towards\n"
            "Lightweight, Adaptive Graph-based Threat\n"
            "Detection\n"
            "Alex Rivera1, Morgan Chen2, Taylor Smith3\n"
            "1Northbridge University, 2Eastbank Institute\n"
            "Abstract--This is the body, not the title."
        )
        document = extract_document(data, "research.pdf", kind="paper")
        self.assertEqual(
            document.title,
            "Adapting Policies to Signals: Towards Lightweight, Adaptive Graph-based Threat Detection",
        )
        self.assertEqual(document.extraction_method, "pdf_text")
        self.assertNotIn("Alex Rivera", document.title)

    def test_paper_skips_generic_proceedings_cover_before_multiline_title(self):
        data = pdf_fixture(
            "This paper is included in the Proceedings of the\n"
            "35th Example Security Symposium.\n"
            "August 13-15, 2026, Harbor City\n"
            "978-1-111111-11-1\n"
            "Open access to the Proceedings of the\n"
            "35th Example Security Symposium is sponsored by Example Association.\n"
            "HERMES: Reliable Attribution in\n"
            "Graph-based Event Analysis Systems\n"
            "Alex Rivera, Northbridge University; Morgan Chen, Eastbank Institute\n"
            "https://example.org/conference/paper"
        )
        self.assertEqual(
            extract_document(data, "research.pdf", kind="paper").title,
            "HERMES: Reliable Attribution in Graph-based Event Analysis Systems",
        )

    def test_paper_repairs_metadata_containing_only_the_first_title_line(self):
        data = pdf_fixture(
            "Adapting Policies to Signals: Towards\n"
            "Lightweight, Adaptive Graph-based Threat\n"
            "Detection\nAlex Rivera, Morgan Chen\nAbstract",
            title="Adapting Policies to Signals: Towards",
        )
        document = extract_document(data, "research.pdf", kind="paper")
        self.assertEqual(
            document.title,
            "Adapting Policies to Signals: Towards Lightweight, Adaptive Graph-based Threat Detection",
        )
        self.assertEqual(document.extraction_method, "pdf_text")

    def test_paper_keeps_two_word_and_university_titles_and_stops_at_single_author(self):
        for title in (
            "Graph Learning",
            "University Collaboration Networks",
            "Abstract Interpretation for Secure Systems",
            "What Are We Optimizing For",
        ):
            with self.subTest(title=title):
                data = pdf_fixture(title + "\nAlex Rivera\nNorthbridge University\nAbstract")
                self.assertEqual(extract_document(data, "research.pdf", kind="paper").title, title)

    def test_paper_accepts_lowercase_continuation_after_unfinished_title(self):
        data = pdf_fixture(
            "A lightweight approach to\n"
            "adapting models in production\n"
            "A. Researcher, B. Scientist\nExample University\nAbstract"
        )
        self.assertEqual(
            extract_document(data, "research.pdf", kind="paper").title,
            "A lightweight approach to adapting models in production",
        )

    def test_paper_does_not_extend_title_with_body_sentences_or_replace_unrelated_metadata(self):
        text = "Reliable Systems for Researchers\nThese results show how a system behaves."
        self.assertEqual(
            extract_document(pdf_fixture(text), "research.pdf", kind="paper").title,
            "Reliable Systems for Researchers",
        )
        self.assertEqual(
            extract_document(
                pdf_fixture(text, title="Validated Registry Title"), "research.pdf", kind="paper"
            ).title,
            "Validated Registry Title",
        )

    def test_pdf_preserves_useful_metadata_when_body_has_no_text(self):
        document = extract_document(pdf_fixture("", title="연구실 논문 제목"), "upload.pdf")
        self.assertEqual(document.title, "연구실 논문 제목")
        self.assertEqual(document.extraction_method, "pdf_metadata")
        self.assertEqual(document.text, "")
        self.assertTrue(document.warning)

    def test_only_first_three_pdf_pages_are_read(self):
        document = extract_document(
            pdf_fixture("First Paper Title", "Second page", "Third page", "Excluded unique words"),
            "paper.pdf",
        )
        self.assertIn("Third page", document.text)
        self.assertNotIn("Excluded", document.text)

    def test_corrupt_encrypted_and_empty_pdf_remain_searchable(self):
        for data in (b"broken", pdf_fixture("Hidden Title", encrypted=True), pdf_fixture("")):
            with self.subTest(data=data[:10]):
                document = extract_document(data, "연구실_발표.pdf")
                self.assertEqual(document.title, "연구실 발표")
                self.assertEqual(document.extraction_method, "filename")
                self.assertTrue(document.warning)
                self.assertEqual(document.text, "")

    def test_reference_dois_are_not_primary_identifiers(self):
        document = extract_document(
            pdf_fixture(
                "A Paper Title\nReferences\nSome cited paper https://doi.org/10.1111/OTHER\narXiv:2201.00001v2"
            ),
            "paper.pdf",
        )
        self.assertIsNone(document.doi)
        self.assertIsNone(document.arxiv_id)

    def test_multiple_dois_are_ambiguous(self):
        document = extract_document(
            pdf_fixture("Comparison of Papers\n10.1111/ONE\n10.1111/TWO"), "paper.pdf"
        )
        self.assertIsNone(document.doi)

    def test_pptx_preserves_korean_and_prefers_metadata(self):
        document = extract_document(
            pptx_fixture("첫 번째 슬라이드\n논문 분석", title="한국어 연구 발표"), "slides.pptx"
        )
        self.assertEqual(document.title, "한국어 연구 발표")
        self.assertIn("논문 분석", document.text)
        self.assertEqual(document.extraction_method, "pptx_metadata")

    def test_pptx_falls_back_to_cover_and_reads_only_five_slides(self):
        document = extract_document(
            pptx_fixture("실험 결과와 분석", "Second", "Third", "Fourth", "Fifth", "Excluded"),
            "slides.pptx",
        )
        self.assertEqual(document.title, "실험 결과와 분석")
        self.assertIn("Fifth", document.text)
        self.assertNotIn("Excluded", document.text)

    def test_pptx_uses_visible_slide_order(self):
        presentation = '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="2" r:id="second"/><p:sldId id="1" r:id="first"/></p:sldIdLst></p:presentation>'
        relationships = '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="first" Target="slides/slide1.xml"/><Relationship Id="second" Target="slides/slide2.xml"/></Relationships>'
        document = extract_document(
            pptx_fixture(
                "First stored slide",
                "Visible cover slide",
                extras={
                    "ppt/presentation.xml": presentation,
                    "ppt/_rels/presentation.xml.rels": relationships,
                },
            ),
            "slides.pptx",
        )
        self.assertEqual(document.title, "Visible cover slide")

    def test_zip_bomb_member_is_not_expanded(self):
        document = extract_document(
            pptx_fixture("Title", extras={"docProps/core.xml": "x" * (MAX_XML_BYTES + 1)}),
            "large.pptx",
        )
        self.assertEqual(document.title, "large")
        self.assertEqual(document.extraction_method, "filename")
        self.assertIn("용량 제한", document.warning)

    def test_xml_entities_are_rejected(self):
        xml = '<!DOCTYPE foo [<!ENTITY custom "expanded">]><foo>&custom;</foo>'
        document = extract_document(
            pptx_fixture("Title", extras={"docProps/core.xml": xml}), "unsafe.pptx"
        )
        self.assertEqual(document.extraction_method, "filename")
        self.assertTrue(document.warning)

    def test_arxiv_old_ids_and_doi_parentheses(self):
        document = extract_document(
            pdf_fixture(
                "Scientific Title\narXiv:HEP-TH/9901001v4\nhttps://doi.org/10.1002/(SICI)1234."
            ),
            "paper.pdf",
        )
        self.assertEqual(document.arxiv_id, "hep-th/9901001")
        self.assertEqual(document.doi, "10.1002/(sici)1234")

    def test_unicode_search_ignores_width_case_spacing_and_punctuation(self):
        self.assertEqual(normalize_search("ＡＩ - 한국어_연구.pdf"), "ai한국어연구pdf")
        self.assertIn(normalize_search("한국어 연구"), normalize_search("ＡＩ - 한국어_연구.pdf"))
        self.assertEqual(normalize_search("한글"), normalize_search("한글"))
        self.assertEqual(normalize_title("BERT: A Model"), normalize_title("bert — a_model"))

    def test_legacy_ppt_is_recorded_with_unicode_filename_and_checksum(self):
        # Legacy PowerPoint uses the binary Compound File format, not ZIP/XML.
        data = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1\x00legacy binary slide content"
        for filename in ("연구실_발표자료.ppt", "연구실_발표자료.PPT"):
            with self.subTest(filename=filename):
                document = extract_document(data, filename)
                self.assertEqual(document.title, "연구실 발표자료")
                self.assertEqual(document.sha256, hashlib.sha256(data).hexdigest())
                self.assertEqual(document.extraction_method, "filename")
                self.assertEqual(document.text, "")
                self.assertIsNone(document.doi)
                self.assertIsNone(document.arxiv_id)
                self.assertEqual(
                    document.warning,
                    ".ppt 원본을 보관했습니다. 본문 분석은 PDF/PPTX 형식에서 지원합니다.",
                )

    def test_unsupported_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "PDF, PPT, PPTX"):
            extract_document(b"example", "note.txt")


class DocumentWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_subprocess_preserves_paper_default_and_slides_visual_policy(self):
        data = styled_pdf_fixture([("Visible Cover Title", 36, 80, 650)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "document.pdf"
            path.write_bytes(data)
            paper = await extract_saved(path, "document.pdf", data)
            slides = await extract_saved(path, "document.pdf", data, kind="slides")
        self.assertEqual(paper.title, "Old Template Title")
        self.assertEqual(slides.title, "Visible Cover Title")
        self.assertEqual(paper.sha256, slides.sha256)


if __name__ == "__main__":
    unittest.main()

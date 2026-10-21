from __future__ import annotations

import hashlib
import io
import unittest
import zipfile
from xml.sax.saxutils import escape

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

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


class DocumentTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

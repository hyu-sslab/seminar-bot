from __future__ import annotations

import unittest

from seminar_bot.documents import DocumentInfo
from seminar_bot.submissions import (
    MIN_PAPERS,
    classify_document,
    count_submission,
    submission_summary,
)


def document(**changes) -> DocumentInfo:
    values = {
        "title": "A Research Topic",
        "text": "Introduction and experimental results",
        "doi": None,
        "arxiv_id": None,
        "sha256": "a" * 64,
        "extraction_method": "pdf_text",
    }
    values.update(changes)
    return DocumentInfo(**values)


class ClassificationTests(unittest.TestCase):
    def test_powerpoint_formats_always_count_as_presentation_material(self):
        for filename in ("talk.ppt", "talk.PPTX", "논문.ppt", "paper.pptx"):
            with self.subTest(filename=filename):
                result = classify_document(filename, document(), "논문")
                self.assertEqual(result.kind, "slides")

    def test_explicit_slide_filenames_override_embedded_paper_identifiers(self):
        info = document(doi="10.1234/example", arxiv_id="2401.00001")
        for filename in (
            "slides.pdf",
            "2026_presentation.PDF",
            "발표자료.pdf",
            "발표 자료.pdf",
            "홍길동_발표_자료.pdf",
            "발표-자료-final.pdf",
            "논문_슬라이드.pdf",
            "paper-slides.pdf",
        ):
            with self.subTest(filename=filename):
                self.assertEqual(classify_document(filename, info, "논문").kind, "slides")

    def test_paper_filenames_are_recognized(self):
        for filename in ("paper.pdf", "paper_1.pdf", "2026-paper-final.pdf", "참고논문_1.pdf"):
            with self.subTest(filename=filename):
                self.assertEqual(classify_document(filename, document()).kind, "paper")

    def test_ambiguous_pdf_stays_unknown(self):
        for filename in ("document.pdf", "attention.pdf", "연구실_발표.pdf", "paperclip.pdf"):
            with self.subTest(filename=filename):
                self.assertEqual(classify_document(filename, document()).kind, "unknown")

    def test_identifiers_suggest_paper_for_otherwise_ambiguous_pdf(self):
        for info in (document(doi="10.1234/example"), document(arxiv_id="2401.00001")):
            with self.subTest(info=info):
                self.assertEqual(classify_document("upload.pdf", info).kind, "paper")

    def test_only_exact_message_labels_apply_to_all_uploads(self):
        for message, kind in (
            ("논문", "paper"),
            ("  논문\n", "paper"),
            ("발표자료", "slides"),
            ("발표 자료", "slides"),
            ("발표\t자료", "slides"),
            ("슬라이드", "slides"),
            ("발표자료와 논문입니다", "unknown"),
            ("논문 3개와 발표 자료 올립니다", "unknown"),
            ("발표 자료: upload.pdf, 논문: research.pdf", "unknown"),
            ("논문을 올립니다", "unknown"),
        ):
            with self.subTest(message=message):
                self.assertEqual(classify_document("upload.pdf", document(), message).kind, kind)

    def test_explicit_slide_message_overrides_paper_filename(self):
        self.assertEqual(classify_document("paper.pdf", document(), "발표자료").kind, "slides")

    def test_unsupported_extension_cannot_be_counted_as_a_paper(self):
        result = classify_document("paper.docx", document(doi="10.1234/example"), "논문")
        self.assertEqual(result.kind, "unknown")

    def test_filename_patterns_inspect_filename_without_parent_directories(self):
        for filename in ("slides/upload.pdf", "C:\\논문\\upload.pdf"):
            with self.subTest(filename=filename):
                self.assertEqual(classify_document(filename, document()).kind, "unknown")

    def test_normalized_filename_recognizes_fullwidth_extension_and_keyword(self):
        result = classify_document("ＳＬＩＤＥＳ.ＰＤＦ", document())
        self.assertEqual(result.kind, "slides")


class SubmissionSummaryTests(unittest.TestCase):
    def test_empty_submission_requests_both_requirements(self):
        result = submission_summary({})
        self.assertIn("발표 자료 1개 이상 + 서로 다른 논문 PDF 3개 이상", result)
        self.assertIn("현재: 발표 자료 0개 · 논문 PDF 0개", result)
        self.assertIn("제출 대기", result)
        self.assertIn("서로 다른 논문 PDF 3개", result)

    def test_three_papers_and_slides_complete_submission(self):
        result = submission_summary({"slides": 1, "papers": MIN_PAPERS, "unknown": 0})
        self.assertIn("제출 완료", result)
        self.assertNotIn("제출 대기", result)

    def test_unknown_files_do_not_replace_missing_papers(self):
        result = submission_summary({"slides": 1, "papers": 2, "unknown": 4})
        self.assertIn("서로 다른 논문 PDF 1개가 더 필요", result)
        self.assertIn("분류 확인 대기: 4개", result)
        self.assertNotIn("제출 완료", result)

    def test_papers_alone_do_not_complete_submission(self):
        result = submission_summary({"slides": 0, "papers": 5, "complete": True})
        self.assertIn("발표 자료 1개가 더 필요", result)
        self.assertNotIn("제출 완료", result)

    def test_extra_files_do_not_prevent_completion(self):
        result = submission_summary({"slides": 2, "papers": 4, "unknown": 1})
        self.assertIn("제출 완료", result)
        self.assertIn("분류 확인 대기: 1개", result)


class SubmissionCountTests(unittest.TestCase):
    def test_empty_submission(self):
        self.assertEqual(
            count_submission([]),
            {"slides": 0, "papers": 0, "unknown": 0, "complete": False, "missing_papers": 3},
        )

    def test_slides_and_three_distinct_papers_complete_submission(self):
        records = [{"kind": "slides", "filename": "slides.pptx", "sha256": "slides"}]
        records.extend(
            {"kind": "paper", "filename": f"paper-{i}.pdf", "sha256": f"paper-{i}"}
            for i in range(3)
        )
        self.assertEqual(
            count_submission(records),
            {"slides": 1, "papers": 3, "unknown": 0, "complete": True, "missing_papers": 0},
        )

    def test_same_file_with_different_names_fills_one_slot(self):
        records = [
            {"kind": "paper", "filename": "original.pdf", "sha256": "same"},
            {"kind": "paper", "filename": "renamed.pdf", "sha256": "same"},
            {"kind": "slides", "filename": "slides.ppt", "sha256": "presentation"},
            {"kind": "slides", "filename": "copy.ppt", "sha256": "presentation"},
        ]
        result = count_submission(records)
        self.assertEqual(result["papers"], 1)
        self.assertEqual(result["slides"], 1)
        self.assertEqual(result["missing_papers"], 2)
        self.assertFalse(result["complete"])

    def test_doi_arxiv_and_sha_matches_merge_transitively(self):
        records = [
            {"kind": "paper", "filename": "a.pdf", "sha256": "a", "doi": "10.1234/EXAMPLE"},
            {
                "kind": "paper",
                "filename": "b.pdf",
                "sha256": "b",
                "arxiv_id": "2401.00001v2",
            },
            {
                "kind": "paper",
                "filename": "c.pdf",
                "sha256": "c",
                "doi": "https://doi.org/10.1234/example",
                "arxiv_id": "arXiv:2401.00001v3",
            },
            {"kind": "paper", "filename": "d.pdf", "sha256": "c"},
        ]
        self.assertEqual(count_submission(records)["papers"], 1)

    def test_same_useful_metadata_title_matches_despite_separator_case_changes(self):
        records = [
            {
                "kind": "paper",
                "filename": "a.pdf",
                "sha256": "a",
                "title": "Attention Is All You Need",
                "extraction_method": "pdf_metadata",
            },
            {
                "kind": "paper",
                "filename": "b.pdf",
                "sha256": "b",
                "title": "attention-is-all-you-need",
                "extraction_method": "pdf_metadata",
            },
        ]
        self.assertEqual(count_submission(records)["papers"], 1)

    def test_filename_fallback_or_generic_metadata_title_does_not_merge_papers(self):
        for method, title in (
            ("filename", "Useful Looking Research Title"),
            ("pdf_text", "Useful Looking Research Title"),
            ("pdf_metadata", "Untitled"),
            ("pdf_metadata", "Paper"),
            ("pdf_metadata", "paper.pdf"),
            ("pdf_metadata", "Microsoft Word - document"),
        ):
            with self.subTest(method=method, title=title):
                records = [
                    {
                        "kind": "paper",
                        "filename": f"{index}.pdf",
                        "sha256": f"{index}",
                        "title": title,
                        "extraction_method": method,
                    }
                    for index in range(3)
                ]
                self.assertEqual(count_submission(records)["papers"], 3)

    def test_short_shared_metadata_titles_do_not_merge_distinct_papers(self):
        records = [
            {
                "kind": "paper",
                "filename": f"{index}.pdf",
                "sha256": f"{index}",
                "doi": f"10.1234/study-{index}",
                "title": "Study",
                "extraction_method": "pdf_metadata",
            }
            for index in range(2)
        ]
        self.assertEqual(count_submission(records)["papers"], 2)

    def test_presentation_bytes_cannot_also_count_as_a_paper(self):
        records = [
            {"kind": "slides", "filename": "slides.pdf", "sha256": "same"},
            {"kind": "paper", "filename": "paper.pdf", "sha256": "same"},
        ]
        result = count_submission(records)
        self.assertEqual(result["slides"], 1)
        self.assertEqual(result["papers"], 0)

    def test_paper_role_requires_pdf_and_slide_role_requires_supported_extension(self):
        records = [
            {"kind": "paper", "filename": "paper.pptx", "sha256": "a"},
            {"kind": "paper", "filename": "paper.doc", "sha256": "b"},
            {"kind": "slides", "filename": "slides.docx", "sha256": "c"},
        ]
        result = count_submission(records)
        self.assertEqual(result["papers"], 0)
        self.assertEqual(result["slides"], 0)

    def test_unknown_count_deduplicates_bytes_and_never_completes_submission(self):
        records = [
            {"kind": "unknown", "filename": "a.pdf", "sha256": "a"},
            {"kind": "unknown", "filename": "renamed.pdf", "sha256": "a"},
            {"kind": "unknown", "filename": "b.pdf", "sha256": "b"},
        ]
        result = count_submission(records)
        self.assertEqual(result["unknown"], 2)
        self.assertEqual(result["papers"], 0)
        self.assertFalse(result["complete"])

    def test_missing_optional_fields_remain_separate_without_identity_evidence(self):
        records = [{"kind": "paper", "filename": f"{index}.pdf"} for index in range(3)]
        self.assertEqual(count_submission(records)["papers"], 3)


if __name__ == "__main__":
    unittest.main()

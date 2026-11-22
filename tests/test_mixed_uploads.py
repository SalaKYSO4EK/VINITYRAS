from io import BytesIO
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "model_bundle")]

from uploads import InvalidUpload, stage_pdfs
from run_inference import file_outcomes
from article_analyzer.photovoltaic_tables import detect_pdf
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject


PDF = b"%PDF-1.7\n"


def zip_data(entries):
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return data.getvalue()


class MixedUploadsTest(unittest.TestCase):
    def test_retains_valid_direct_pdf_and_archive_member(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            issues = []
            archive = zip_data([("../escape.pdf", PDF), ("inside/malformed.pdf", b"bad"),
                                ("ok/nested.pdf", PDF), ("ok/DIRECT.PDF", PDF), ("notes.docx", b"info"),
                                ("__MACOSX/hidden.txt", b"metadata"), ("ok/._nested.pdf", b"metadata")])
            names = stage_pdfs([("direct.pdf", PDF), ("notes.txt", b"not an article"), ("broken.zip", b"bad"),
                                ("case.zip", archive), ("..pdf", PDF)], path, file_issues=issues)
            self.assertEqual(names, ["direct.pdf", "nested.pdf"])
            self.assertEqual({entry.name for entry in path.iterdir()}, set(names))
            self.assertEqual([(issue.get("archive"), issue["file"]) for issue in issues],
                             [(None, "notes.txt"), (None, "broken.zip"), ("case.zip", "../escape.pdf"),
                              ("case.zip", "inside/malformed.pdf"), ("case.zip", "ok/DIRECT.PDF"),
                              ("case.zip", "notes.docx"), (None, "..pdf")])

    def test_crc_failure_does_not_keep_partial_pdf_or_hide_next_member(self):
        broken_data = PDF + b"A" * 10_000
        archive = bytearray(zip_data([("retry.pdf", broken_data), ("next.pdf", PDF)]))
        offset = archive.index(broken_data)
        archive[offset + 6500] = ord("B")
        with TemporaryDirectory() as directory:
            path = Path(directory)
            issues = []
            names = stage_pdfs([("case.zip", archive), ("retry.pdf", PDF)], path, file_issues=issues)
            self.assertEqual(names, ["next.pdf", "retry.pdf"])
            self.assertEqual((path / "retry.pdf").read_bytes(), PDF)
            self.assertEqual([(issue["archive"], issue["file"]) for issue in issues], [("case.zip", "retry.pdf")])

    def test_all_rejected_identifies_each_input(self):
        with TemporaryDirectory() as directory:
            with self.assertRaises(InvalidUpload) as error:
                stage_pdfs([("notes.docx", b"text"), ("archive.zip", zip_data([("readme.md", b"text")]))],
                           Path(directory))
            self.assertEqual(error.exception.status, 400)
            self.assertEqual([issue["file"] for issue in error.exception.file_issues], ["notes.docx", "readme.md", "archive.zip"])
            self.assertEqual(error.exception.file_issues[1]["archive"], "archive.zip")
            self.assertIn("нет файлов PDF", error.exception.file_issues[2]["reason"])
            self.assertTrue(all(issue["reason"] for issue in error.exception.file_issues))

    def test_readable_without_findings_has_distinct_outcome(self):
        pairs = [(name, None) for name in ("damaged.pdf", "empty.pdf", "useful.pdf", "layout.pdf")]
        result = {"documents": [{"file": "empty.pdf", "title": {"title": "Readable metadata"}, "records": []},
                                {"file": "useful.pdf", "records": [{"value": "42"}]}],
                  "errors": [{"file": "layout.pdf", "phase": "layout"}]}
        outcomes = file_outcomes(pairs, ["damaged.pdf"], result)
        self.assertEqual([item["status"] for item in outcomes], ["unreadable", "no_findings", "found", "processing_failed"])

    def test_detects_image_only_page_but_not_blank_page(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "scan.pdf"
            writer = PdfWriter()
            page = writer.add_blank_page(100, 100)
            writer.add_blank_page(100, 100)
            image = DecodedStreamObject()
            image.set_data(b"q 100 0 0 100 0 0 cm BI /W 1 /H 1 /BPC 8 /CS /G ID \x00 EI Q")
            page[NameObject("/Contents")] = writer._add_object(image)
            writer.write(path)
            self.assertEqual(detect_pdf(path)["image_only_pages"], [1])

    def test_image_and_custom_failure_are_limited_and_requested_title_is_found(self):
        pairs = [(name, None) for name in ("scan.pdf", "custom.pdf", "title.pdf")]
        result = {"documents": [{"file": "scan.pdf", "records": [], "image_only_pages": [2, 3]},
                                {"file": "custom.pdf", "records": []},
                                {"file": "title.pdf", "records": [], "title": {"title": "Test", "status": "extracted"}}],
                  "errors": [], "custom": {"limited_sources": ["custom.pdf"]}}
        outcomes = file_outcomes(pairs, [], result, title_requested=True)
        self.assertEqual([item["status"] for item in outcomes], ["processing_limited", "processing_limited", "found"])
        self.assertIn("2, 3", outcomes[0]["reason"])


if __name__ == "__main__":
    unittest.main()

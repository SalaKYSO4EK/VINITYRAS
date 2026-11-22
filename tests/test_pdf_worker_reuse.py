import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model_bundle"))
from article_analyzer.pdf_lab import run_device_stage
from article_analyzer.pdf_title_evidence import printed_metadata_title, printed_outline_title


class PrintedPage:
    height = 800

    def __init__(self, title, *, top=170):
        self.title = title
        self.chars = self.line(title, top, 16, "Heading") + self.line("Body of the article. " * 10, 450, 9, "Body")

    @staticmethod
    def line(text, top, size, font):
        return [{"text": letter, "x0": 20 + i * 6, "x1": 26 + i * 6, "top": top,
                 "bottom": top + size, "size": size, "fontname": font} for i, letter in enumerate(text)]

    def crop(self, box):
        class Crop:
            chars = [c for c in self.chars if box[1] <= c["top"] and c["bottom"] <= box[3]]

            def extract_text(child, **kwargs):
                return "".join(c["text"] for c in child.chars)
        return Crop()


class PdfWorkerReuseTest(unittest.TestCase):
    def test_root_bookmark_needs_the_complete_visible_opening_heading(self):
        title = "A verified title of a published paper"
        page = PrintedPage(title)
        page.chars.extend(page.line(title, 540, 9, "Body"))
        root = {"/Title": title, "/First": {}}
        pdf = SimpleNamespace(trailer={"/Root": {"/Outlines": {"/First": root}}},
                              pages=[SimpleNamespace(extract_text=lambda: title + "\nAuthors")])
        with TemporaryDirectory() as folder, patch("pypdf.PdfReader", return_value=pdf):
            path = Path(folder) / "article.pdf"
            path.touch()
            match = printed_outline_title(page, path)
            self.assertEqual(match["source_page"], 1)
            self.assertEqual(match["source_fragment"], title)
            self.assertEqual(match["method"], "root_bookmark_verified_against_opening_heading")
            root["/Next"] = {}
            self.assertIsNone(printed_outline_title(page, path))
            del root["/Next"]
            self.assertIsNone(printed_outline_title(PrintedPage(title + " with a subtitle"), path))
            pdf.pages[0].extract_text = lambda: "Introduction\n" + title
            self.assertIsNone(printed_outline_title(page, path))

    def test_metadata_must_be_entire_prominent_printed_heading(self):
        title = "Tuning the bandgap without compromising efficiency"
        page = PrintedPage(title)
        verified = printed_metadata_title(page, title)
        self.assertEqual(verified["title"], title)
        self.assertEqual(verified["source_fragment"], title)
        self.assertEqual(verified["source_page"], 1)
        self.assertIsNone(printed_metadata_title(page, title.replace("Tuning the", "Tuningthe")))
        self.assertIsNone(printed_metadata_title(page, title.removesuffix(" efficiency")))
        self.assertIsNone(printed_metadata_title(PrintedPage(title, top=6), title))
        page.chars.extend(page.line(title, 540, 9, "Body"))
        self.assertIsNone(printed_metadata_title(page, title))

    def test_interrupted_shared_title_stage_does_not_lose_device_pass(self):
        with TemporaryDirectory() as temp:
            out = Path(temp)
            pdf = out / "D001.pdf"
            sample = {"page": 5, "table": "TABLE 2", "sample": "Pristine"}
            doc = {"file": pdf.name, "publication_id": pdf.stem, "device_inventory": {"known": [sample]}}
            requests = []

            def worker(module, log, args, timeout, errors, **kwargs):
                payload = json.loads(args[0].read_text(encoding="utf-8"))
                requests.append(payload)
                if "titles" in payload:
                    log.write_text("interrupted base stage", encoding="utf-8")
                    return
                args[1].write_text(json.dumps({"publications": {"D001": [dict(sample, status="completed")]}}),
                                   encoding="utf-8")

            with patch("article_analyzer.pdf_lab.launch_model", side_effect=worker):
                run_device_stage([pdf], [doc], ["architecture"], out, 4, [], out, [pdf])
            self.assertEqual(len(requests), 2)
            self.assertIn("titles", requests[0])
            self.assertNotIn("titles", requests[1])
            self.assertEqual(requests[0]["documents"], requests[1]["documents"])
            self.assertEqual(doc["device_attributes"][0]["sample"], "Pristine")
            self.assertTrue((out / "nuextract-title-worker.log").is_file())

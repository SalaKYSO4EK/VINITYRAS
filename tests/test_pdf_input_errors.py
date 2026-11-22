"""An unreadable article has its own visible error without losing valid siblings."""
from contextlib import redirect_stderr
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory, TemporaryFile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pypdf import PdfWriter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "model_bundle"), str(ROOT / "backend")]

import jobs
import pdf_criteria_runner
import run_inference
from article_analyzer.criteria import Source


class PDFInputErrorTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.bad = self.folder / "corrupt.pdf"
        self.bad.write_bytes(b"%PDF-1.7\ncorrupt test, missing PDF catalogue\n%%EOF\n")
        self.request = self.folder / "request.json"
        self.out = self.folder / "output"
        self.runner = ROOT / "model_bundle/run_inference.py"
        self.request.write_text(json.dumps({
            "schema_version": 1, "job_id": "a" * 32, "criteria": "PCE",
            "files": [{"name": self.bad.name, "path": str(self.bad)}],
            "criteria_pdf": {"name": "criteria.pdf", "path": str(self.folder / "criteria.pdf")},
        }), "utf-8")

    def test_cli_reports_damaged_article_before_model_load(self):
        for main, flags in (
            (run_inference.main, []),
            (pdf_criteria_runner.main, ["--runner", str(self.runner)]),
        ):
            with self.subTest(main=main.__module__), TemporaryDirectory() as temp:
                out = Path(temp) / "output"
                command = ["inference", *flags, "--request", str(self.request), "--out", str(out)]
                with (
                    TemporaryFile(mode="w+", encoding="utf-8") as stderr,
                    redirect_stderr(stderr),
                    patch.object(sys, "argv", command),
                    patch("article_analyzer.inference_job.contain_workers"),
                    patch.object(pdf_criteria_runner, "read_criteria",
                                 return_value=(Source("criteria.pdf", "", {2: "PCE"}), [])),
                ):
                    status = main()
                    stderr.seek(0)
                    message = stderr.read()
                self.assertEqual(status, jobs.PDF_INPUT_EXIT_CODE)
                self.assertIn("PDF_INPUT_ERROR:", message)
                self.assertIn("corrupt.pdf", message)
                self.assertEqual(list(out.iterdir()), [])

    def test_good_pdf_still_staged_when_sibling_is_corrupt(self):
        good = self.folder / "D001.pdf"
        pdf = PdfWriter()
        pdf.add_blank_page(600, 800)
        with good.open("wb") as stream:
            pdf.write(stream)
        warnings = []
        paths, rejected = run_inference.stage_inputs(
            [(self.bad.name, self.bad), (good.name, good)], self.folder / "staged", warnings,
        )
        self.assertEqual(rejected, [self.bad.name])
        self.assertEqual(paths[0].read_bytes(), good.read_bytes())
        self.assertIn(self.bad.name, warnings[0])

    def test_queue_keeps_safe_damaged_file_reason(self):
        manager = jobs.Jobs(self.folder / "jobs", self.runner, Path(sys.executable), timeout=10)
        status = jobs.PDF_INPUT_EXIT_CODE

        def spawn(command, *, stdout, **options):
            stdout.write(b"PDF_INPUT_ERROR: sensitive test path must not be returned to the user")
            return SimpleNamespace(returncode=status, poll=lambda: status)

        with patch.object(jobs.subprocess, "Popen", side_effect=spawn), self.assertRaises(jobs.JobFailure) as raised:
            manager._invoke(self.folder, self.out)
        self.assertEqual(raised.exception.code, "invalid_pdf")
        self.assertIn("Не удалось прочитать загруженные PDF", str(raised.exception))
        self.assertNotIn("sensitive test path", str(raised.exception))


if __name__ == "__main__":
    unittest.main()

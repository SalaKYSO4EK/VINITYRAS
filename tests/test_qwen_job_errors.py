"""Ensure a safe Qwen API failure reaches the queue with either criteria format."""
from contextlib import redirect_stderr
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory, TemporaryFile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "model_bundle"), str(ROOT / "backend")]

import jobs
import pdf_criteria_runner
import run_inference
from article_analyzer.criteria import Source
from article_analyzer.qwen_api import QwenApiError
from pypdf import PdfWriter


MESSAGE = "Qwen API не принял ключ. Проверьте настройки ключа."
TEXT_CRITERIA = "Сопоставь значение КПД из статей."


class QwenJobErrorTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        article = self.folder / "D001.pdf"
        pdf = PdfWriter()
        pdf.add_blank_page(600, 800)
        with article.open("wb") as stream:
            pdf.write(stream)
        self.article = article
        self.request = self.folder / "request.json"
        self.out = self.folder / "output"
        self.runner = ROOT / "model_bundle" / "run_inference.py"

    def save_request(self, use_pdf):
        data = {"schema_version": 1, "job_id": "d" * 32, "criteria": TEXT_CRITERIA,
                "files": [{"name": self.article.name, "path": str(self.article)}]}
        if use_pdf:
            data["criteria_pdf"] = {"name": "criteria.pdf", "path": str(self.folder / "criteria.pdf")}
        self.request.write_text(json.dumps(data, ensure_ascii=False), "utf-8")

    def call_main(self, main, *args):
        command = ["runner", *args, "--request", str(self.request), "--out", str(self.out)]
        with (TemporaryFile(mode="w+", encoding="utf-8") as stderr, redirect_stderr(stderr),
              patch.object(sys, "argv", command),
              patch.dict(os.environ, {"NEXUS_LEGACY_INFERENCE": "1"}),
              patch("article_analyzer.inference_job.contain_workers"),
              patch("article_analyzer.pdf_lab.compile_criteria", side_effect=QwenApiError(MESSAGE))):
            status = main()
            stderr.seek(0)
            return status, stderr.read()

    def test_text_criteria_reports_reserved_failure_instead_of_using_fallback(self):
        self.save_request(use_pdf=False)
        result = self.call_main(run_inference.main)
        self.assertEqual(result, (3, f"QWEN_API_ERROR: {MESSAGE}\n"))
        self.assertEqual(list(self.out.iterdir()), [])

    def test_pdf_criteria_reports_same_reserved_failure(self):
        self.save_request(use_pdf=True)
        with patch.object(pdf_criteria_runner, "read_criteria",
                          return_value=(Source("criteria.pdf", "unused", {2: TEXT_CRITERIA}), [])):
            result = self.call_main(pdf_criteria_runner.main, "--runner", str(self.runner))
        self.assertEqual(result, (3, f"QWEN_API_ERROR: {MESSAGE}\n"))
        self.assertFalse((self.folder / "criteria-error.txt").exists())
        self.assertEqual(list(self.out.iterdir()), [])

    def test_bad_criteria_pdf_still_has_its_own_error(self):
        self.save_request(use_pdf=True)
        status, message = self.call_main(pdf_criteria_runner.main, "--runner", str(self.runner))
        self.assertEqual(status, 1)
        self.assertIn("Не удалось открыть PDF с критериями.", message)
        self.assertEqual((self.folder / "criteria-error.txt").read_text("utf-8"), message.strip())

    def queue_failure(self, exit_code, payload, use_pdf=False):
        manager = jobs.Jobs(self.folder / "jobs", self.runner, Path(sys.executable), timeout=10)
        job_id = "a" * 32
        directory = manager.root / job_id
        directory.mkdir(exist_ok=True)
        jobs.write_json(directory / "status.json", {"job_id": job_id, "status": "running"})
        if use_pdf:
            (directory / "criteria.pdf").touch()

        def spawn(command, *, stdout, **options):
            if use_pdf:
                self.assertIn("--runner", command)
            stdout.write(payload)
            return SimpleNamespace(returncode=exit_code, poll=lambda: exit_code)

        with patch.object(jobs.subprocess, "Popen", side_effect=spawn), self.assertRaises(jobs.JobFailure) as raised:
            manager._invoke(directory, directory / "output")
        manager._fail(job_id, raised.exception)
        return manager.status(job_id)["error"]

    def test_queue_keeps_safe_message_for_text_and_pdf(self):
        log = ("Earlier output\n" * 1000 + f"QWEN_API_ERROR: {MESSAGE}\n").encode("utf-8")
        expected = {"code": "qwen_api_error", "message": MESSAGE}
        self.assertEqual(self.queue_failure(3, log), expected)
        self.assertEqual(self.queue_failure(3, log, use_pdf=True), expected)

    def test_queue_ignores_marker_for_a_different_exit_code(self):
        error = self.queue_failure(1, f"QWEN_API_ERROR: {MESSAGE}\n".encode("utf-8"))
        self.assertEqual(error["code"], "runner_failed")
        self.assertNotIn(MESSAGE, error["message"])

    def test_reserved_exit_without_valid_marker_uses_fixed_message(self):
        error = self.queue_failure(3, b"QWEN_API_ERROR: \xff\nunspecified traceback data")
        self.assertEqual(error, {"code": "qwen_api_error", "message": jobs.QWEN_API_DEFAULT_MESSAGE})


if __name__ == "__main__":
    unittest.main()

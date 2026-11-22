"""Polling uses measured worker progress and retains completed timings."""

import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(PROJECT / "backend"), str(PROJECT / "model_bundle")]

from jobs import JobFailure, Jobs, public_status, write_json
from article_analyzer.job_progress import report_phase, report_qwen_request


class JobProgressTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        root = Path(folder.name)
        self.clock = 1000.0
        clock = patch("jobs.time.time", side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.jobs = Jobs(root / "jobs", root / "runner.py", Path(sys.executable), 90)
        self.pdf = b"%PDF-1.7\n"
        self.accepted = self.jobs.enqueue("Критерии", [("A.pdf", self.pdf), ("B.pdf", self.pdf)],
                                          upload_bytes=1214, upload_seconds=0.25)
        self.job_id = self.accepted["job_id"]
        self.directory = self.jobs.root / self.job_id
        assignment = patch.dict(os.environ, NEXUS_JOB_PROGRESS=str(self.directory / "progress.json"))
        assignment.start()
        self.addCleanup(assignment.stop)

    def view(self):
        return public_status(self.directory, self.jobs.status(self.job_id))

    def test_initial_queue_uses_actual_upload_bytes_and_unpacked_count(self):
        self.clock += 11
        progress = self.view()["progress"]
        self.assertEqual(progress["phase"], "queued")
        self.assertEqual(progress["elapsed_seconds"], 11)
        self.assertEqual(progress["uploaded_pdf_count"], 2)
        self.assertEqual(progress["pdf_bytes"], 2 * len(self.pdf))
        self.assertEqual(progress["upload_bytes"], 1214)
        self.assertEqual(progress["phase_seconds"]["receive_upload"], 0.25)
        self.assertIn("stage_uploads", progress["phase_seconds"])
        self.assertNotIn("pdf_index", progress)

    def test_worker_file_numbers_do_not_reset_phase_and_measurements_survive_completion(self):
        self.clock = 1003
        self.jobs._progress(self.directory, "starting", status="running")
        self.clock = 1005
        report_phase("read_pdfs", pdf_index=1, pdf_total=2, file="A.pdf")
        self.assertEqual(self.view()["progress"]["file"], "A.pdf")
        self.clock = 1007
        report_phase("criteria")
        report_qwen_request(103)
        self.clock = 1008
        report_qwen_request(210)
        self.clock = 1009
        report_phase("extract_pdf", pdf_index=1, pdf_total=2, file="A.pdf")
        self.clock = 1018
        report_phase("extract_pdf", pdf_index=2, pdf_total=2, file="B.pdf")
        self.clock = 1022
        progress = self.view()["progress"]
        self.assertEqual((progress["pdf_index"], progress["pdf_total"], progress["file"]), (2, 2, "B.pdf"))
        self.assertEqual(progress["elapsed_seconds"], 13)
        self.assertEqual(progress["qwen_requests"], 2)
        self.assertEqual(progress["qwen_request_bytes"], 313)
        self.assertEqual(progress["phase_seconds"]["starting"], 2)
        self.assertEqual(progress["phase_seconds"]["queued"], 3)
        self.assertEqual(progress["phase_seconds"]["read_pdfs"], 2)
        self.assertEqual(progress["phase_seconds"]["criteria"], 2)
        self.jobs._progress(self.directory, "publish")
        self.clock = 1024
        self.jobs._progress(self.directory, "completed", status="completed")
        progress = self.view()["progress"]
        self.assertEqual(progress["phase_seconds"]["extract_pdf"], 13)
        self.assertEqual(progress["phase_seconds"]["publish"], 2)
        self.assertEqual(progress["qwen_request_bytes"], 313)
        self.assertNotIn("file", progress)
        self.assertNotIn("elapsed_seconds", progress)

    def test_failure_preserves_last_worker_phase(self):
        self.jobs._progress(self.directory, "starting", status="running")
        self.clock = 1002
        report_phase("read_criteria_pdf")
        self.clock = 1015
        self.jobs._fail(self.job_id, JobFailure("criteria_pdf_error", "Не удалось прочитать PDF."))
        state = self.view()
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["progress"]["failed_phase"], "read_criteria_pdf")
        self.assertEqual(state["progress"]["phase_seconds"]["read_criteria_pdf"], 13)

    def test_cache_hit_preserves_flag_and_explicit_zero_calls(self):
        self.jobs._progress(self.directory, "starting", status="running")
        self.clock = 1001
        self.jobs._progress(self.directory, "cache_hit", reused=True)
        self.clock = 1002
        self.jobs._progress(self.directory, "publish")
        self.jobs._progress(self.directory, "completed", status="completed")
        state = self.view()
        self.assertTrue(state["reused"])
        self.assertEqual(state["progress"]["phase_seconds"]["cache_hit"], 1)
        self.assertEqual(state["progress"]["qwen_requests"], 0)
        self.assertEqual(state["progress"]["qwen_request_bytes"], 0)

    def test_invalid_progress_cannot_break_status_or_fake_stage(self):
        self.jobs._progress(self.directory, "starting", status="running")
        write_json(self.directory / "progress.json",
                   {"phase": "extract_pdf", "worker_started_at": 998, "phase_started_at": 999, "phase_seconds": {}})
        self.assertEqual(self.view()["progress"]["phase"], "starting")
        write_json(self.directory / "progress.json",
                   {"phase": "extract_pdf", "worker_started_at": 1000, "phase_started_at": "broken", "phase_seconds": {}})
        self.assertEqual(self.view()["progress"]["phase"], "starting")


if __name__ == "__main__":
    unittest.main()

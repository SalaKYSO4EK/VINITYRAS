"""Verify the explicit solar CSV contract before creating the user archive."""
import csv
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from jobs import (SOLAR_SUBMISSION_FORMAT, SOLAR_TEMPLATE_CSV, SOLAR_TEMPLATE_DIR,
                  JobFailure, Jobs, archive_files, inspect_output)
from reference_check import REPORT_FILE


class SolarArchiveTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.job = self.root / "job"
        self.output = self.job / "output"
        self.output.mkdir(parents=True)
        self.inputs = self.job / "input"
        self.inputs.mkdir()
        for name in ("P078.pdf", "P094.PDF"):
            (self.inputs / name).write_bytes(b"staged input")
        self.jobs = Jobs(self.root / "runs", Path("unused.py"), Path(sys.executable), 30)
        for name in SOLAR_TEMPLATE_CSV:
            with (SOLAR_TEMPLATE_DIR / name).open(encoding="utf-8-sig", newline="") as stream:
                fields = next(csv.reader(stream))
            with (self.output / name).open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                if name == "publications.csv":
                    writer.writerows([{"publication_id": stem} for stem in ("P078", "P094")])
        self.marker(SOLAR_SUBMISSION_FORMAT)

    def marker(self, submission_format=None):
        document = {"profile": "dual-qwen", "warnings": [], "file_outcomes": []}
        if submission_format:
            document["submission_format"] = submission_format
        (self.output / "report.json").write_text(json.dumps(document), encoding="utf-8")

    def test_archive_keeps_empty_template_tables_and_comparison_report(self):
        (self.output / "extras.csv").write_text("property\nlayer stack\n", encoding="utf-8")
        (self.output / "empty_extras.csv").write_text("property\n", encoding="utf-8")
        audit = {"matched_files": ["P078.pdf"], "tables": {"publications.csv": {"status":"match"}}}
        with patch("jobs.reference_check.compare", return_value=audit) as compare:
            self.jobs._publish(self.job, self.output)
        compare.assert_called_once_with(self.inputs, self.output)
        published = json.loads((self.job / "result.json").read_bytes())
        self.assertEqual(published["submission_format"], SOLAR_SUBMISSION_FORMAT)
        with ZipFile(self.job / "submission.zip") as archive:
            self.assertCountEqual(archive.namelist(), SOLAR_TEMPLATE_CSV | {"extras.csv", REPORT_FILE})
            self.assertEqual(archive.read("measurements.csv"), (self.output / "measurements.csv").read_bytes())

    def test_unmarked_dual_output_keeps_only_populated_csvs(self):
        self.marker()
        result = inspect_output(self.output)
        self.assertEqual(archive_files(self.output, result), ["publications.csv"])
        self.assertNotIn("submission_format", result)

    def test_marked_incomplete_or_nonstandard_csv_cannot_publish(self):
        path = self.output / "materials.csv"
        path.unlink()
        with self.assertRaisesRegex(ValueError, "отсутствует materials.csv"):
            inspect_output(self.output)
        path.write_text("publication_id,material_id\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Заголовки materials.csv"):
            inspect_output(self.output)

    def test_requires_exactly_one_publication_for_each_input_pdf(self):
        (self.inputs / "extra.pdf").write_bytes(b"staged input without publication")
        with patch("jobs.reference_check.compare") as compare, self.assertRaises(JobFailure) as raised:
            self.jobs._publish(self.job, self.output)
        self.assertEqual(raised.exception.code, "invalid_result")
        compare.assert_not_called()
        self.assertFalse((self.job / "submission.zip").exists())

    def test_no_archive_when_the_marked_tables_are_all_header_only(self):
        target = self.output / "publications.csv"
        header = target.read_text(encoding="utf-8").splitlines()[0]
        target.write_text(header + "\n", encoding="utf-8")
        self.assertEqual(archive_files(self.output, inspect_output(self.output)), [])


if __name__ == "__main__":
    unittest.main()

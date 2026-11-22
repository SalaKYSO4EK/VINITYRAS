"""Exercise the validation gate independently of model inference."""
import csv
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from jobs import JobFailure, Jobs
from reference_check import IDS, REPORT_FILE, compare, file_hash


class ReferenceCheckTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.directory = self.root / "job"
        self.reference = self.root / "reference"
        self.inputs = self.directory / "input"
        self.output = self.directory / "output"
        for path in (self.reference, self.inputs, self.output):
            path.mkdir(parents=True)
        self.paper = self.inputs / "P078.pdf"
        self.paper.write_bytes(b"exact solar PDF")

        self.write(self.reference, "devices.csv", ["publication_id", "device_id", "device_label"],
                   [["P078", "A", "4-Ni-NG/CE"], ["P082", "B", "Another solar sample"]])
        self.write(self.reference, "results.csv", ["publication_id", "result_id", "device_id", "metric_name",
                   "source_page", "source_table", "metric_value", "note"],
                   [["P078", "R1", "A", "PCE", "7", "Table 2", "10.90", "long note"],
                    ["P082", "R1", "B", "PCE", "2", "Table 1", "19", "another PDF"]])
        for name in IDS.keys() - {"devices.csv", "results.csv"}:
            headers = ["publication_id", "title"] if name == "publications.csv" else ["publication_id", IDS[name]]
            rows = [["P078", "Confirmed title"]] if name == "publications.csv" else []
            self.write(self.reference, name, headers, rows)
        self.manifest = {"schema_version": 1, "revision": "check-1",
                         "pdfs": {self.paper.name: file_hash(self.paper), "P082.pdf": "a" * 64}}
        self.pin()
        self.write(self.output, "devices.csv", ["publication_id", "device_id", "device_label"],
                   [["P078", "DV09", "4-Ni-NG/CE"], ["P079", "X", "Outside corpus"]])
        self.write(self.output, "results.csv", ["publication_id", "result_id", "device_id", "metric_name",
                   "source_page", "source_table", "metric_value"],
                   [["P078", "777", "DV09", "PCE", "7", "Table2", "10.9"],
                    ["P079", "1", "X", "FF", "2", "Table 5", "999"]])

    @staticmethod
    def write(folder, name, fields, rows):
        with (folder / name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(rows)

    def pin(self):
        self.manifest["tables"] = {name: file_hash(self.reference / name) for name in IDS}
        (self.reference / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def test_matching_semantic_rows_and_mixed_input(self):
        (self.inputs / "P079.pdf").write_bytes(b"other paper")
        audit = compare(self.inputs, self.output, self.reference)
        self.assertEqual(audit["matched_files"], ["P078.pdf"])
        self.assertEqual(audit["other_files"], ["P079.pdf"])
        results = audit["tables"]["results.csv"]
        self.assertEqual((results["matches"],results["reference_rows"],results["only_reference"]), (1,1,0))
        self.assertEqual(audit["tables"]["devices.csv"]["status"], "match")
        self.paper.write_bytes(b"same name, different paper")
        self.assertIsNone(compare(self.inputs, self.output, self.reference))

    def test_reports_changed_value_and_duplicate_without_replacing_values(self):
        csv_path = self.output / "results.csv"
        content = csv_path.read_text("utf-8").replace("10.9", "11.2")
        csv_path.write_text(content, encoding="utf-8")
        result = compare(self.inputs, self.output, self.reference)["tables"]["results.csv"]
        self.assertEqual(result["changed"], 1)
        self.assertEqual(result["examples"][0]["fields"], {"metric_value": {"current": "11.2", "reference": "10.9"}})
        self.assertEqual(csv_path.read_text("utf-8"), content)
        csv_path.write_text(content + "P078,778,DV09,PCE,7,Table2,11.2\n", encoding="utf-8")
        result = compare(self.inputs, self.output, self.reference)["tables"]["results.csv"]
        self.assertEqual((result["ambiguous_groups"], result["only_current"], result["only_reference"]), (1, 2, 1))

    def test_skips_empty_tables_and_reports_changed_scope(self):
        self.write(self.output, "issues.csv", ["publication_id", "issue_id", "check_name"], [])
        self.write(self.output, "publications.csv", ["publication_id", "wrong_field"], [["P078", "x"]])
        audit = compare(self.inputs, self.output, self.reference)
        self.assertEqual(audit["tables"]["issues.csv"]["status"], "empty_current")
        self.assertEqual(audit["tables"]["publications.csv"]["status"], "schema_mismatch")

    def test_publishes_fresh_audit_and_withholds_when_pinned_reference_is_damaged(self):
        jobs = Jobs(self.root / "runs", Path("unneeded.py"), Path(sys.executable), 30)
        csv_path = self.output / "results.csv"
        before = csv_path.read_bytes()
        with patch("reference_check.REFERENCE", self.reference):
            jobs._publish(self.directory, self.output)
            with ZipFile(self.directory / "submission.zip") as archive:
                self.assertEqual(json.loads(archive.read(REPORT_FILE))["reference"], "check-1")
                self.assertEqual(archive.read("results.csv"), before)
            self.manifest["revision"] = "check-2"
            self.pin()
            jobs._publish(self.directory, self.output)
            audit = json.loads((self.output / REPORT_FILE).read_bytes())
            self.assertEqual(audit["reference"], "check-2")
            (self.reference / "results.csv").write_text("corrupted", encoding="utf-8")
            with self.assertLogs(level="ERROR"), self.assertRaises(JobFailure) as failure:
                jobs._publish(self.directory, self.output)
            self.assertEqual(failure.exception.code, "reference_check_failed")
            self.assertEqual(csv_path.read_bytes(), before)

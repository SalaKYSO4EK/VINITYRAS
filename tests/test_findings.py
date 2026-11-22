"""Verify page ownership and deduplication across saved output formats."""
import csv
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from findings import findings_for_source


class FindingsTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name)

    def csv(self, name, rows):
        with (self.output / name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=rows[0])
            writer.writeheader()
            writer.writerows(rows)

    def counts(self, source):
        result = findings_for_source(self.output, source)
        return [(page["page"], mark["kind"], mark["count"]) for page in result["pages"] for mark in page["marks"]]

    def test_p078_repeated_context_and_comparison_projection(self):
        # The two PCE columns for 4-Ni-NG/CE are separate cells; the methods citations are shared.
        room = {"page": 3, "text": "All J-V tests were performed under ambient conditions at room temperature."}
        light = {"page": 6, "start": 1528, "text": "AM 1.5 (100 mW cm/C02)"}
        context = {"temperature": {"source": room}, "pin": {"source": light}, "spectrum": {"source": light},
                   "aggregation": {"scope": "column", "source": {"page": 7, "text": "PCE max (%)"}}}
        rows = [{"result_id": rid, "source_file": "P078.pdf", "page": 7, "metric": "PCE",
                 "value_raw": value, "unit_raw": "%", "sample": "4-Ni-NG/CE", "context": context}
                for rid, value in (("R0024", "10.90"), ("R0025", "11.74"))]
        records = {"documents": [{"file": "P078.pdf", "records": rows},
                                 {"file": "another.pdf", "records": []}], "records": rows}
        (self.output / "records.json").write_text(json.dumps(records), encoding="utf-8")
        projection = [{"publication_id": "P078", "result_id": row["result_id"], "source_page": 7,
                       "metric_name": "PCE", "metric_value": row["value_raw"]} for row in rows]
        self.csv("results.csv", projection)
        self.csv("Efficiency.csv", projection)
        self.assertEqual(self.counts("P078.pdf"), [(3, "methods", 1), (6, "illumination", 1), (7, "values", 2)])
        self.assertEqual(self.counts("another.pdf"), [])

    def test_generic_csv_requires_exact_file_and_physical_page(self):
        row = {"publication_id": "D011", "source_file": "D011.pdf", "source_page": "6",
               "source_fragment": "top-K = 1", "top_k": "1", "verification_status": "needs_review"}
        self.csv("top_k.csv", [row, {**row, "source_file": "variant.pdf", "source_page": "2"},
                               {**row, "source_page": "0"}, {**row, "source_page": "vi"},
                               {**row, "source_page": "2", "source_fragment": ""}])
        self.assertEqual(self.counts("D011.pdf"), [(6, "records", 1)])
        self.assertEqual(self.counts("variant.pdf"), [(2, "records", 1)])

    def test_legacy_results_and_text_only_projection_without_records(self):
        base = {"publication_id": "P078", "result_id": "R1", "source_page": 7,
                "metric_value": "10.90", "metric_name": "PCE", "source_fragment": "10.90"}
        self.csv("results.csv", [base])
        self.csv("Efficiency.csv", [base, {**base, "result_id": "", "source_page": 2, "source_fragment": "PCE of 10.90%"}])
        self.assertEqual(self.counts("P078.pdf"), [(2, "values", 1), (7, "values", 1)])

    def test_root_records_with_no_documents_preserve_zero(self):
        rows = [{"publication_id": "D", "result_id": "R1", "metric": "shift", "page": 4, "value_raw": 0}]
        (self.output / "records.json").write_text(json.dumps({"records": rows}), "utf-8")
        self.assertEqual(self.counts("D.pdf"), [(4, "values", 1)])

    def test_normalized_unit_value_and_spread_are_shown_on_the_same_scale(self):
        base = {"page": 7, "publication_id": "P", "metric": "Jsc", "value_raw": "18.99", "spread_raw": "1.04",
                "unit_raw": "mA cm (cid:4)2", "sources": {"cell": {"text": "18.99 ± 1.04"}},
                "normalized": {"status": "normalized", "value": "18.99", "spread_value": "1.04", "unit": "mA/cm2"}}
        ff = {**base, "metric": "FF", "value_raw": "0.54", "spread_raw": "0.03", "unit_raw": "fraction",
              "sources": {"cell": {"text": "0.54 ± 0.03"}},
              "normalized": {"status": "normalized", "value": "54.00", "spread_value": "3.00", "unit": "%"}}
        missing = {**ff, "result_id": "R3", "normalized": {**ff["normalized"], "spread_value": ""}}
        ambiguous = {**ff, "result_id": "R4", "normalized": {**ff["normalized"], "status": "convention"}}
        (self.output / "records.json").write_text(json.dumps({"records": [base, ff, missing, ambiguous]}), "utf-8")
        mark = findings_for_source(self.output, "P.pdf")["pages"][0]["marks"][0]
        self.assertEqual(mark["details"], ["Jsc 18.99 ± 1.04 mA/cm2", "FF 54.00 ± 3.00 %",
                                          "FF 0.54 ± 0.03 fraction", "FF 0.54 ± 0.03 fraction"])

    def test_expand_lists_every_distinct_cell_even_if_the_text_coincides(self):
        rows = [{"publication_id": "P078", "result_id": f"R{i}", "source_page": 7, "source_table": "Table 2",
                 "device_label": "4-Ni-NG/CE", "metric_name": "PCE", "metric_value": "10.90",
                 "metric_unit": "%", "spread_value": "0.63", "aggregation": "mean"} for i in range(5)]
        rows.append({**rows[0], "result_id": "R5", "metric_value": "11.74", "spread_value": "", "aggregation": "champion"})
        self.csv("Efficiency.csv", rows)
        mark = findings_for_source(self.output, "P078.pdf")["pages"][0]["marks"][0]
        self.assertEqual(mark["count"], 6)
        self.assertEqual(len(mark["details"]), 6)
        self.assertEqual(mark["details"][0], "Table 2 · 4-Ni-NG/CE · PCE 10.90 ± 0.63 % · среднее")
        self.assertEqual(mark["details"][-1], "Table 2 · 4-Ni-NG/CE · PCE 11.74 % · максимум")

    def test_unreadable_metadata_returns_csv_and_warning(self):
        (self.output / "records.json").write_text("broken", "utf-8")
        self.csv("results.csv", [{"publication_id": "D", "result_id": "R", "source_page": 1,
                                  "metric_name": "PCE", "metric_value": "0"}])
        result = findings_for_source(self.output, "D.pdf")
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(result["pages"][0]["page"], 1)
        self.assertEqual(self.counts("missing.pdf"), [])


if __name__ == "__main__":
    unittest.main()

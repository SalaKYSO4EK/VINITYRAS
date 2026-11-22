"""Check the request contracts for unchanged base tables and custom extensions."""
import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from article_analyzer.efficiency_export import comparison_metrics, write_comparison
from article_analyzer.criteria import parse_field_page
from article_analyzer.pdf_lab import default_criteria, TEMPLATE
from run_inference import custom_plan, request_data


class RequestProfilesTest(unittest.TestCase):
    def test_qwen_field_identifiers_survive_distinct_display_labels(self):
        raw = json.dumps({"groups": [{"name": "items", "row": "one item", "fields": [
            {"item_id": "PCE", "item_name": "Power Conversion Efficiency"},
            {"item_id": "Voc", "item_name": "Open Circuit Voltage"},
        ]}]})
        group, = parse_field_page(raw).groups
        self.assertEqual([field.name for field in group.field_specs()], ["PCE", "Voc"])

    def test_blank_criteria_uses_exact_template_headers(self):
        with TemporaryDirectory() as folder:
            work = Path(folder)
            request = work / "request.json"
            data = {"schema_version": 1, "job_id": "test", "files": [{"name": "Article.pdf",
                                                                     "path": str(work / "Article.pdf")}]}
            for value in (None, "", " \t  "):
                if value is not None:
                    data["criteria"] = value
                request.write_text(json.dumps(data), encoding="utf-8")
                self.assertEqual(request_data(request)[0]["criteria"], "")
            groups = default_criteria(work)["plan"]["extraction"]["groups"]
            self.assertEqual(len(groups), 7)
            for group in groups:
                with (TEMPLATE / f'{group["name"]}.csv').open(encoding="utf-8-sig", newline="") as source:
                    self.assertEqual([field["name"] for field in group["fields"]], next(csv.reader(source)))

    def test_comparisons_identify_the_scientific_metric(self):
        self.assertEqual(comparison_metrics("Сопоставь значения КПД из статей, где есть эта информация"), ["PCE"])
        self.assertEqual(comparison_metrics("Сравни Voc, Jsc и FF"), ["Voc", "Jsc", "FF"])
        self.assertEqual(comparison_metrics("Извлеки top_k"), [])

    def test_recognized_solar_fields_can_coexist_with_a_custom_group(self):
        groups = [{"name": "results", "fields": [{"name": "pce"}]},
                  {"name": "deposition", "fields": [{"name": "deposition_pressure"}]}]
        compiled = {"plan": {"extraction": {"groups": groups, "nuextract_template":
                    {"results": [{"pce": ""}], "deposition": [{"deposition_pressure": ""}]}}},
                    "other_fields": ["deposition_pressure"]}
        self.assertEqual(custom_plan(compiled, True)["extraction"]["groups"], [groups[1]])
        self.assertEqual(custom_plan(compiled, False)["extraction"]["groups"], groups)

    def test_missing_and_unreadable_are_different_and_make_an_empty_csv(self):
        with TemporaryDirectory() as folder:
            export = Path(folder)
            (export / "publications.csv").write_text(
                "publication_id,title\nA,Readable\nB,Unreadable\n", encoding="utf-8")
            documents = [
                {"publication_id": pid, "file": pid + ".pdf", "records": [],
                 "coverage": {"PCE": {"status": status}}}
                for pid, status in (("A", "not_found_in_recognized_tables"), ("B", "processing_failed"))
            ]
            result = write_comparison(export, documents, "PCE", [], [])
            self.assertEqual(result["publication_status"], {"A": "not_found", "B": "processing_failed"})
            with (export / "Efficiency.csv").open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertIn("source_page", reader.fieldnames)
                self.assertEqual(list(reader), [])


if __name__ == "__main__":
    unittest.main()

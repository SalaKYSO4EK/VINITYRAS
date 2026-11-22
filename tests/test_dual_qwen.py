import csv
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "model_bundle"), str(ROOT / "backend")]

from article_analyzer.dual_qwen import (base_schema, cell_reply, checked_rows, reconcile, requested_pages, run,
                                       selected_rows, solar_schema, write_export)
from article_analyzer.dual_table import survey
from jobs import archive_files, inspect_output
from pypdf import PdfWriter


PLAN = {"results":["publication_id","result_id","metric_name","metric_value","metric_unit",
                   "source_page","source_fragment"],"devices":["publication_id","device_label"]}
TEXT = "PCE (%)     Voc (V)\nalpha    10.90    0.89\nbeta     11.74    0.91"


def rows(*, value="10.90", quote="10.90", row_quote="alpha 10.90 0.89"):
    return checked_rows({"rows":[{"table":"results","label":"alpha","row_quote":row_quote,"fields":[
        {"name":"metric_name","value":"PCE","quote":"PCE (%)"},
        {"name":"metric_value","value":value,"quote":quote},
        {"name":"metric_unit","value":"%","quote":"PCE (%)"},
    ]}]},TEXT,7,PLAN)


class DualQwenTest(unittest.TestCase):
    def test_same_pdf_number_on_a_different_row_does_not_pass(self):
        fields = {row["field"] for row in rows(value="11.74",quote="11.74")}
        self.assertNotIn("metric_value",fields)

    def test_same_numeric_value_may_have_different_decimals_and_excerpt_length(self):
        agree, disputes = reconcile(rows(),rows(value="10.9",row_quote="alpha 10.90"))
        self.assertEqual(len(agree),3)
        self.assertFalse(disputes)

    def test_one_recheck_exports_disputed_value_with_a_note(self):
        initial = rows()
        disagrees = rows(value="0.89",quote="0.89")
        stable = [r for r in initial if r["field"]!="metric_value"]
        self.assertEqual(len(reconcile(initial,disagrees)[1]),1)
        calls = []

        def fake_analyze(path, criteria, plan, lane, completion, retry_pages=None):
            calls.append((lane,retry_pages))
            return {"rows": stable if retry_pages else initial if lane=="a" else disagrees,
                    "errors":[], "pages":[7],"input_sha256":"same"}

        with TemporaryDirectory() as folder, patch("article_analyzer.dual_qwen.base_schema",return_value=PLAN), \
                patch("article_analyzer.dual_qwen.analyze",side_effect=fake_analyze):
            directory = Path(folder)
            self.assertEqual(run([directory/"P078.pdf"],"",directory,object(),object()),{"results":1})
            self.assertCountEqual(calls,[("a",None),("b",None),("a",[7]),("b",[7])])
            with (directory/"results.csv").open(encoding="utf-8",newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual((row["metric_value"], row["source_fragment"]), ("10.90", "10.90"))
            self.assertIn("Спорные данные", row["note"])

    def test_export_only_nonempty_csv_and_preserve_source(self):
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            self.assertEqual(write_export([("P078","P078.pdf",rows())],PLAN,directory),{"results":1})
            (directory/"report.json").write_text(json.dumps(
                {"profile":"dual-qwen","warnings":["review"],"file_outcomes":[]}),"utf-8")
            with (directory/"results.csv").open(encoding="utf-8",newline="") as stream:
                row = next(csv.DictReader(stream))
            self.assertEqual((row["source_page"],row["source_fragment"]),("7","10.90"))
            self.assertEqual(archive_files(directory,inspect_output(directory)),["results.csv"])

    def test_solar_run_formats_narrow_request_and_keeps_each_input_publication(self):
        evidence = rows()
        for row in evidence:
            row["page"] = 1
        fake = {"rows": evidence, "errors": [], "pages": [1], "input_sha256": "same"}
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            input_dir = directory / "input"
            output = directory / "output"
            input_dir.mkdir()
            output.mkdir()
            paper = input_dir / "P078.pdf"
            pdf = PdfWriter()
            pdf.add_blank_page(600, 800)
            pdf.write(paper)
            with patch("article_analyzer.dual_qwen.interpret", return_value={"results":["metric_value"]}), \
                    patch("article_analyzer.dual_qwen.analyze", return_value=fake):
                counts = run([paper], "PCE", output, object(), object(), rejected=["P079.pdf"])
            self.assertEqual(counts["results"], 1)
            self.assertEqual(counts["publications"], 2)
            self.assertEqual(json.loads((output/"report.json").read_bytes())["submission_format"],"solar-v1")
            for name, fields in solar_schema().items():
                with (output/(name+".csv")).open(encoding="utf-8", newline="") as stream:
                    reader = csv.DictReader(stream)
                    self.assertEqual(reader.fieldnames,fields)
                    data = list(reader)
                if name == "results":
                    self.assertEqual((data[0]["metric_value"], data[0]["source_fragment"]), ("10.90","10.90"))
                if name == "publications":
                    self.assertEqual([row["publication_id"] for row in data], ["P078","P079"])
                    self.assertFalse(any(row["title"] for row in data))
            self.assertIn("ошибок: 0", (output/"check-submission.txt").read_text("utf-8"))
            self.assertEqual(archive_files(output,inspect_output(output)), ["results.csv"])

    def test_explicit_rag_uses_its_own_format_even_with_a_solar_keyword(self):
        schema = base_schema("Сравни RAG-исследования, в которых встречается PCE")
        self.assertIn("configurations",schema)
        self.assertNotIn("devices",schema)

    def test_optional_physical_page_request(self):
        self.assertEqual(requested_pages("Возьми значения на физической странице 7–9"),{7,8,9})
        self.assertIsNone(requested_pages("Заполни результаты"))

    def test_center_and_maximum_of_same_metric_remain_separate(self):
        text = "Table 2\nComparison\nDevice   PCEavg(%)   PCEmax(%)\nalpha   10.90 ± 0.63   11.74"
        catalog = survey(text)
        plan = {"results":[*PLAN["results"],"aggregation","spread_value"]}
        selected = selected_rows({"ids":[*catalog,"bad"]}, catalog,7,plan)
        self.assertEqual(len({row["anchor"] for row in selected}),2)
        with TemporaryDirectory() as folder:
            directory = Path(folder)
            self.assertEqual(write_export([("P078","P078.pdf",selected)],plan,directory),{"results":2})
            with (directory/"results.csv").open(encoding="utf-8",newline="") as stream:
                result = list(csv.DictReader(stream))
            self.assertEqual([(r["metric_value"],r["aggregation"],r["spread_value"]) for r in result],
                             [("10.90","mean","0.63"),("11.74","champion","")])

    def test_recheck_can_replace_a_disputed_value_with_same_source(self):
        left,right=rows(),rows(value="0.89",quote="0.89")
        agree,pending=reconcile(left,right)
        initially_disputed=next(iter(pending))
        self.assertEqual(initially_disputed[-1],"metric_value")
        repeated,pending=reconcile(left,left)
        self.assertEqual(repeated[initially_disputed]["value"],"10.90")
        self.assertFalse(pending)

    def test_distinct_requested_metrics_are_distinct_qwen_subtasks(self):
        text = "Table 2\nComparison\nDevice   PCEavg(%)   Voc(V)\nalpha   10.90 ± 0.63   0.89"
        catalog = survey(text)
        seen = []

        def completed(system, prompt, schema, limit):
            data = json.loads(prompt)
            seen.append(data["subtask_metric"])
            return json.dumps({"ids":[c[0] for c in data["cells"]]}), 0

        answer = cell_reply(completed,catalog,"Извлеки все PCE и Voc","a",False)
        self.assertEqual(seen,["PCE","Voc"])
        self.assertEqual(set(answer["ids"]),set(catalog))
        self.assertEqual(answer["ids"],answer["expected_ids"])

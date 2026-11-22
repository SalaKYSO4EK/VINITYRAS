"""Targeted checks for independent extraction and source-based reconciliation."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model_bundle"))

from article_analyzer import inference_generic
from article_analyzer.criteria import ground_article_record
from article_analyzer.evidence import EvidenceIndex
from article_analyzer.generic_consensus import verify_group

SOURCE_PATH = Path("paper.pdf")


def report(evidence, *candidates):
    pairs = [ground_article_record(candidate, evidence, index) for index, candidate in enumerate(candidates)]
    return {"records": [row for row, _ in pairs], "citation_checks": [check for _, check in pairs]}


def print_cell(row, label, value):
    return {"text": value, "row_id": (0, row), "source_table": "Table 1", "source_row": label,
            "source_column": "Efficiency", "source_bbox": "100.00,20.00,130.00,25.00"}


class GenericConsensusTest(unittest.TestCase):
    def test_agreement_uses_exact_original_case_and_page(self):
        evidence = EvidenceIndex({3: "The experiment used an Open Circuit."})
        first = report(evidence, {"method": "open circuit"})
        second = report(evidence, {"method": "OPEN CIRCUIT"})
        rows, _ = verify_group(first, second, ["method"], SOURCE_PATH, 3, evidence, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["method"], "Open Circuit")
        self.assertEqual(rows[0]["source_page"], 3)
        self.assertEqual(rows[0]["source_fragment"], "Open Circuit")
        self.assertEqual(rows[0]["verification_status"], "agreed")

    def test_disagreement_can_be_resolved_from_printed_row_and_column(self):
        evidence = EvidenceIndex({1: "Sample Efficiency\nA 10.90\nB 11.74"})
        first = report(evidence, {"sample": "A", "efficiency": "10.90"})
        second = report(evidence, {"sample": "A", "efficiency": "11.74"})
        rows, review = verify_group(first, second, ["sample", "efficiency"], SOURCE_PATH, 1, evidence,
                                    [print_cell(0, "A", "10.90"), print_cell(1, "B", "11.74")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["efficiency"], "10.90")
        self.assertEqual(rows[0]["source_row"], "A")
        self.assertIn("A 10.90", rows[0]["source_fragment"])
        self.assertEqual(rows[0]["verification_status"], "resolved_from_source")
        self.assertEqual(review["rows"][0]["fields"]["efficiency"]["candidates"],
                         {"first": ["10.90"], "second": ["11.74"]})

    def test_repeated_wrong_agreement_is_rejected_by_table(self):
        evidence = EvidenceIndex({1: "Sample Efficiency\nA 10.90\nB 11.74"})
        same_wrong = report(evidence, {"sample": "A", "efficiency": "11.74"})
        rows, review = verify_group(same_wrong, same_wrong, ["sample", "efficiency"], SOURCE_PATH, 1, evidence,
                                    [print_cell(0, "A", "10.90"), print_cell(1, "B", "11.74")])
        self.assertEqual(rows[0]["efficiency"], "")
        self.assertEqual(rows[0]["verification_status"], "needs_review")
        self.assertEqual(review["rows"][0]["fields"]["efficiency"]["readback"]["text"], "10.90")

    def test_distinct_text_values_stay_blank_after_readback(self):
        evidence = EvidenceIndex({1: "Efficiency was 10.90 before the treatment and 11.74 afterwards."})
        first = report(evidence, {"efficiency": "10.90"})
        second = report(evidence, {"efficiency": "11.74"})
        rows, review = verify_group(first, second, ["efficiency"], SOURCE_PATH, 1, evidence, [])
        self.assertEqual(rows[0]["efficiency"], "")
        self.assertEqual(rows[0]["verification_status"], "needs_review")
        self.assertEqual(set(review["rows"][0]["fields"]["efficiency"]["readback"]), {"10.90", "11.74"})

    def test_unanchored_values_are_only_kept_in_audit(self):
        evidence = EvidenceIndex({1: "No measurements."})
        first = report(evidence, {"efficiency": "10.90", "source_fragment_id": "P1-0"})
        rows, review = verify_group(first, {}, ["efficiency"], SOURCE_PATH, 1, evidence, [])
        self.assertEqual(rows, [])
        self.assertEqual(review["unanchored"]["first"], [{"efficiency": "10.90"}])

    def test_repeated_number_is_bound_to_its_printed_parameter(self):
        evidence = EvidenceIndex({1: "See item 1. Our retriever uses top-K = 1, cosine similarity."})
        both = report(evidence, {"top_k": "1"})
        rows, review = verify_group(both, both, ["top_k"], SOURCE_PATH, 1, evidence, [])
        self.assertEqual(rows[0]["source_fragment"], "top-K = 1")
        self.assertEqual(review["rows"][0]["fields"]["top_k"]["evidence"]["method"], "printed_field_label")

    def test_one_pass_failure_can_be_resolved_by_a_unique_printed_assignment(self):
        evidence = EvidenceIndex({1: "See item 1. Our retriever uses top-K = 1, cosine similarity."})
        first = report(evidence, {"top_k": "1"})
        rows, review = verify_group(first, {}, ["top_k"], SOURCE_PATH, 1, evidence, [])
        self.assertEqual(rows[0]["top_k"], "1")
        self.assertEqual(rows[0]["source_fragment"], "top-K = 1")
        self.assertEqual(review["rows"][0]["fields"]["top_k"]["status"], "resolved_from_source")

    def test_two_printed_parameter_settings_do_not_disambiguate(self):
        evidence = EvidenceIndex({1: "See item 1. Primary retriever top-K = 1. The ablation uses top-k = 3."})
        both = report(evidence, {"top_k": "1"})
        rows, review = verify_group(both, both, ["top_k"], SOURCE_PATH, 1, evidence, [])
        self.assertEqual(rows, [])
        self.assertEqual(review["unanchored"]["first"], [{"top_k": "1"}])

    def test_run_uses_two_fresh_calls_with_same_template(self):
        page = MagicMock()
        page.extract_text.return_value = "Introduction. The method used open circuit."
        page.find_tables.return_value = []
        pdf = MagicMock()
        pdf.__enter__.return_value.pages = [page]
        model = MagicMock(revision="test")
        model.extract.return_value = SimpleNamespace(data={"methods": [
            {"method": "open circuit", "source_fragment_id": "P1-14"}
        ]}, raw_output="test", seconds=0)
        plan = {"extraction": {"groups": [{"name": "methods", "row": "one method",
                                          "fields": [{"name": "method", "instruction": ""}]}],
                               "nuextract_template": {"methods": [{"method": "", "source_fragment_id": ""}]}}}
        with (TemporaryDirectory() as folder, patch.object(inference_generic.pdfplumber, "open", return_value=pdf),
              patch.object(inference_generic, "visible_page", return_value=page),
              patch.object(inference_generic, "report_phase") as progress,
              patch.object(inference_generic, "LocalNuExtract", return_value=model)):
            result = inference_generic.run_generic(iter([SOURCE_PATH]), plan, Path(folder), [])
            csv_output = (Path(folder) / "methods.csv").read_text("utf-8")
        progress.assert_called_once_with("extract_custom", pdf_index=1, pdf_total=1, file="paper.pdf")
        self.assertEqual(model.extract.call_count, 2)
        first, second = (call.args for call in model.extract.call_args_list)
        self.assertEqual(first[1], second[1])
        self.assertNotEqual(first[0], second[0])
        self.assertIn("agreed", csv_output)
        self.assertEqual(set(result["pages"][0]["passes"]), {"first", "second"})
        self.assertEqual(result["verification"]["counts"]["agreed"], 1)
        self.assertEqual(result["confirmed_sources"], ["paper.pdf"])

    def test_two_failed_extractions_mark_file_partial(self):
        page = MagicMock()
        page.extract_text.return_value = "An article with text and a model processing error."
        pdf = MagicMock()
        pdf.__enter__.return_value.pages = [page]
        model = MagicMock(revision="test")
        plan = {"extraction": {"groups": [{"name": "methods", "fields": [{"name": "method"}]}],
                               "nuextract_template": {"methods": [{"method": ""}]}}}
        failed = {"groups": {"methods": {"error": "failed extraction"}}}
        warnings = []
        with (TemporaryDirectory() as folder, patch.object(inference_generic.pdfplumber, "open", return_value=pdf),
              patch.object(inference_generic, "visible_page", return_value=page),
              patch.object(inference_generic, "LocalNuExtract", return_value=model),
              patch.object(inference_generic, "extract_pass", return_value=failed)):
            result = inference_generic.run_generic([SOURCE_PATH], plan, Path(folder), warnings)
        self.assertEqual(result["limited_sources"], ["paper.pdf"])
        self.assertEqual(result["confirmed_sources"], [])
        self.assertFalse(any("не найдено" in warning for warning in warnings))


if __name__ == "__main__":
    unittest.main()

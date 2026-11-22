"""Targeted fixtures for agreement, literal tie-breaking and ambiguous attribution."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model_bundle"))

from article_analyzer.pv_layout_review import compare_layouts, empty_layout_review, review_selected_cell
from article_analyzer.photovoltaic_tables import longitudinal


def printed(chars, value, x, y):
    left = x
    for char in value:
        if char == " ":
            x += 2.4
            continue
        chars.append({"x0": x, "x1": x + 1, "top": y, "bottom": y + 7, "text": char})
        x += 1
    return {"text": value, "bbox": [left, y, x, y + 7]}


class LayoutReviewTest(unittest.TestCase):
    def test_printed_row_sample_box_ends_at_its_glyphs_in_a_dense_table(self):
        chars = []

        def word(value, x, y):
            source = printed(chars, value, x, y)
            x0, top, x1, bottom = source["bbox"]
            return dict(text=value, x0=x0, top=top, x1=x1, bottom=bottom)

        caption = word("Table 1", 10, 2)
        header = [word("Device", 10, 15), word("Voc(V)", 52, 15), word("PCE(%)", 85, 15)]
        body = []
        for label, y in (("A", 30), ("B", 39.5)):
            sample = word(label, 10, y)
            values = [word("0.9", 52, y), word("10.2", 85, y)]
            body.append(([sample, *values], values))
        region = header + [w for row, _ in body for w in row]
        rows = longitudinal(SimpleNamespace(chars=chars), 1, [caption, *region], (8, 14, 120, 50), region, body)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0]["sources"]["sample"], {"text": "A", "bbox": [10, 30, 11, 37]})
        self.assertEqual(rows[2]["sources"]["sample"], {"text": "B", "bbox": [10, 39.5, 11, 46.5]})

    def setUp(self):
        chars = []
        self.sample = printed(chars, "Device A", 10, 30)
        self.alternative_sample = printed(chars, "Device B", 10, 40)
        self.header = printed(chars, "PCE (%)", 85, 20)
        self.cell = printed(chars, "10.90 ± 0.63", 90, 30)
        self.page = SimpleNamespace(chars=chars)

    def row(self, sample=None, spread=""):
        sample = self.sample if sample is None else sample
        sources = {"sample": sample, "header": self.header,
                   "value": {"text": "10.90", "bbox": [90, 30, 95, 37]}}
        if spread:
            sources["cell"] = self.cell
        return {"page": 1, "table": "Table 2", "metric": "PCE", "unit_raw": "%",
                "sample": sample["text"], "condition": "", "value_raw": "10.90",
                "spread_raw": spread, "sources": sources}

    def compare(self, first, second):
        report = empty_layout_review()
        decisions = compare_layouts(self.page, [(None, [first])], [(None, [second], [])], report)
        return review_selected_cell(first, decisions), report

    def test_agreement_counts_once(self):
        row, report = self.compare(self.row(spread="0.63"), self.row(spread="0.63"))
        self.assertEqual(report["counts"]["agreed"], 1)
        self.assertNotIn("ambiguity", row)

    def test_printed_cell_resolves_missing_spread(self):
        row, report = self.compare(self.row(), self.row(spread="0.63"))
        self.assertEqual(report["counts"]["resolved"], 1)
        self.assertEqual(report["differences"][0]["evidence"]["value"], self.cell)
        self.assertEqual(row["spread_raw"], "0.63")
        self.assertEqual(row["sources"]["cell"], self.cell)

    def test_two_sample_bindings_remain_for_review(self):
        row, report = self.compare(self.row(spread="0.63"), self.row(self.alternative_sample, spread="0.63"))
        self.assertEqual(report["counts"]["needs_review"], 1)
        self.assertEqual({item["sample"] for item in row["ambiguity"]["alternatives"]}, {"Device A", "Device B"})

    def test_incomplete_sample_does_not_block_a_supported_row(self):
        absent = self.row({"text": "", "bbox": None}, spread="0.63")
        absent["ambiguity"] = {"reason": "Не найден образец.", "alternatives": []}
        row, report = self.compare(self.row(spread="0.63"), absent)
        self.assertEqual(report["counts"]["single_pass"], 1)
        self.assertNotIn("ambiguity", row)
        self.assertEqual(len(report["versions"]["coordinates"]), 1)


if __name__ == "__main__":
    unittest.main()

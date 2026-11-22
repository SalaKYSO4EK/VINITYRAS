"""Focused layout regressions using PDF-native text and separate coordinates."""
from io import BytesIO
from pathlib import Path
import sys
from unittest import TestCase

import pdfplumber
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model_bundle"))
from article_analyzer.rag_tables import extract_table_cells


def render_layout(layout, page_number=3):
    writer = PdfWriter()
    page = writer.add_blank_page(500, 500)
    name = NameObject
    font = DictionaryObject({name("/Type"): name("/Font"), name("/Subtype"): name("/Type1"),
                             name("/BaseFont"): name("/Helvetica")})
    page[name("/Resources")] = DictionaryObject({
        name("/Font"): DictionaryObject({name("/F1"): writer._add_object(font)}),
    })
    content = DecodedStreamObject()
    content.set_data("\n".join(
        f"BT /F1 9 Tf 1 0 0 1 {x} {500 - y} Tm ({value}) Tj ET" for x, y, value in layout
    ).encode("ascii"))
    page[name("/Contents")] = writer._add_object(content)
    out = BytesIO()
    writer.write(out)
    out.seek(0)
    with pdfplumber.open(out) as pdf:
        return extract_table_cells(pdf.pages[0], page_number)


class RAGTableTest(TestCase):
    def test_multilevel_relationships_and_ambiguous_number_omission(self):
        layout = [
            (50, 35, "Table 3: Evaluations of question answering."),
            (174, 54, "Qwen3"), (154, 66, "HotpotQA"), (258, 66, "MuSiQue"),
            (50, 79, "Methods"), (150, 79, "EM"), (204, 79, "F1"),
            (258, 79, "EM"), (314, 79, "F1"),
            (50, 91, "Dense RAG"), (148, 91, ".204"), (203, 91, ".330"),
            (250, 91, ".142"), (271, 91, ".246"), (313, 91, ".210"),
            (50, 103, "Other RAG"), (148, 103, ".268"), (203, 103, "37.9%"),
            (257, 103, ".216"), (313, 103, ".311"),
        ]
        records = render_layout(layout)
        self.assertEqual(len(records), 7)
        first = records[0]
        self.assertEqual((first["page"], first["table"], first["sample"], first["generator"],
                          first["dataset"], first["metric"], first["raw"], first["unit"]),
                         (3, "Table 3", "Dense RAG", "Qwen3", "HotpotQA", "EM", ".204", ""))
        self.assertEqual(first["fragment"], ".204")
        self.assertEqual(first["header_fragment"], "Qwen3 HotpotQA EM")
        self.assertGreater(first["bbox"][0], first["row_bbox"][2])
        printed_unit = next(row for row in records if row["raw"] == "37.9%")
        self.assertEqual(printed_unit["unit"], "%")
        self.assertFalse(any(row["raw"] in {".142", ".246"} for row in records))

    def test_methods_in_columns_and_shared_caption_metric(self):
        records = render_layout([
            (45, 43, "Benchmark"), (143, 43, "Baseline"), (231, 43, "New Method"),
            (45, 57, "SQuAD"), (143, 57, "34.2%"), (230, 57, "39.2%"),
            (45, 69, "TriviaQA"), (143, 69, "62.0%"), (230, 69, "74.1"),
            (45, 81, "Avg."), (143, 81, "48.1%"), (230, 81, "56.7%"),
            (51, 101, "Table 9: Comparison of RAG factuality scores."),
        ])
        self.assertEqual(len(records), 6)
        outcome = next(record for record in records if record["raw"] == "74.1")
        self.assertEqual((outcome["sample"], outcome["dataset"], outcome["metric"], outcome["unit"]),
                         ("New Method", "TriviaQA", "factuality", ""))
        average = next(record for record in records if record["raw"] == "48.1%")
        self.assertEqual((average["dataset"], average["aggregation"], average["row_fragment"]),
                         ("", "average", "Avg."))

    def test_without_table_caption_does_not_promote_prose_or_chart_labels(self):
        self.assertEqual(render_layout([
            (40, 40, "Graph 9"), (20, 55, "Methods"), (130, 55, "EM"), (200, 55, "F1"),
            (20, 70, "Method A"), (130, 70, ".15"), (200, 70, ".23"),
            (20, 80, "Method B"), (130, 80, ".25"), (200, 80, ".31"),
        ]), [])

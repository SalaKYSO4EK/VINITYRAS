from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

from pypdf import PdfWriter
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model_bundle"))

from article_analyzer.pce_text import find_pce_in_text, text_pce


def make_pdf(path, lines, *, clipped=None):
    writer = PdfWriter()
    name = NameObject
    font = DictionaryObject({name("/Type"): name("/Font"), name("/Subtype"): name("/Type1"),
                             name("/BaseFont"): name("/Helvetica")})
    for line in lines:
        page = writer.add_blank_page(width=612, height=792)
        if line is not None:
            fonts = DictionaryObject({name("/F1"): font})
            resources = DictionaryObject({name("/Font"): fonts})
            page[name("/Resources")] = resources
            contents = DecodedStreamObject()
            instructions = f"BT /F1 12 Tf 45 735 Td ({line}) Tj ET"
            if clipped:
                form = DecodedStreamObject()
                form.set_data(f"BT /F1 12 Tf 2 74 Td ({clipped}) Tj ET".encode("ascii"))
                form.update({
                    name("/Type"): name("/XObject"), name("/Subtype"): name("/Form"),
                    name("/BBox"): ArrayObject([NumberObject(value) for value in (0, 0, 300, 40)]),
                    name("/Resources"): DictionaryObject({name("/Font"): fonts}),
                })
                resources[name("/XObject")] = DictionaryObject({name("/Fm0"): writer._add_object(form)})
                instructions += " q 1 0 0 1 40 620 cm /Fm0 Do Q"
            contents.set_data(instructions.encode("ascii"))
            page.replace_contents(contents)
    writer.write(path)


class PceTextTest(unittest.TestCase):
    def test_retains_exact_multiline_source_and_normalizes_only_decimal_separator(self):
        quote = "Средний КПД составил\n12,40 %."
        self.assertEqual(text_pce({3: quote}), [{
            "metric_name": "PCE", "metric_value": "12.40", "metric_unit": "%",
            "raw_value": "12,40", "raw_unit": "%", "aggregation": "mean",
            "source_page": 3, "source_fragment": quote,
        }])

    def test_explicit_absolute_values_and_aggregation(self):
        examples = (
            ("The best device demonstrated a PCE of 20.01%.", "20.01", "champion"),
            ("We achieved a 14.7% certified PCE.", "14.7", ""),
            ("The power conversion efficiency (PCE) was 8.20%.", "8.20", ""),
            ("The PCE for the test device was 0%.", "0", ""),
        )
        for sentence, value, aggregation in examples:
            with self.subTest(sentence=sentence):
                row, = text_pce({1: sentence})
                self.assertEqual((row["metric_value"], row["aggregation"]), (value, aggregation))

    def test_declines_references_ranges_relative_or_hypothetical_numbers(self):
        for sentence in (
            "The device gave a PCE of 11.5% [12].",
            "Previous studies reported a PCE of 32%.",
            "Chen et al. demonstrated a PCE of 24%.",
            "The test reported a 13% PCE gain.",
            "PCE improved by 15%.",
            "The devices showed PCE of 14% and 17%.",
            "PCE ranged from 14 to 17%.",
            "The devices show 14 – 17% PCE.",
            "The predicted PCE was 13%.",
            "The PCE target is 17%.",
            "The PCE was 110%.",
            "The PCE was -1%.",
            "Table 2. PCE (%)\nDevice A\n14.5",
        ):
            with self.subTest(sentence=sentence):
                self.assertEqual(text_pce({1: sentence}), [])

    def test_deduplicates_repeated_claims_and_stops_at_references(self):
        quote = "We report a PCE of 15.0%."
        data = {
            1: quote,
            2: "PCE was 15%.\n\nThe mean PCE was 15.0%.\nReferences\nPCE was 99%.",
            3: "PCE was 98%.",
        }
        self.assertEqual([(r["source_page"], r["aggregation"]) for r in text_pce(data)], [(1, ""), (2, "mean")])

    def test_reads_physical_page_and_keeps_unavailable_text_separate(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "prose.pdf"
            quote = "The power conversion efficiency of the solar cell was 21.6%."
            make_pdf(path, [None, quote])
            row, = find_pce_in_text(path)
            self.assertEqual((row["source_page"], row["source_fragment"]), (2, quote))
            blank = path.with_name("blank.pdf")
            make_pdf(blank, [None])
            with self.assertRaisesRegex(ValueError, "нет доступного"):
                find_pce_in_text(blank)

    def test_ignores_prose_outside_the_visible_pdf_form(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "clipped.pdf"
            make_pdf(path, ["This page has no measured efficiency."], clipped="PCE was 99%.")
            self.assertEqual(find_pce_in_text(path), [])

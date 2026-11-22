from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"model_bundle"))

from article_analyzer.quick_pv import requested_metrics, text_candidates


class QuickPVTest(unittest.TestCase):
    def test_short_metrics_and_source_request_uses_fast_profile(self):
        self.assertEqual(requested_metrics("Извлеки PCE и Voc и укажи источники в PDF"),("PCE","Voc"))
        for text in ("Извлеки PCE на странице 7", "Извлеки PCE и Voc для образца CE",
                     "Извлеки PCE, Voc и материалы", "Извлеки PCE только со средним и разбросом"):
            with self.subTest(text=text):
                self.assertEqual(requested_metrics(text),())

    def test_uses_physical_page_and_measured_units(self):
        pages=[(7,"The fabricated device showed 0.55% power conversion efficiency.\n"
                  "The open-circuit voltage was 203.98 mV. A pseudo efficiency of 7.6% was shown.\n"
                  "The Voc loss was 0.35 V.\nReferences\nPaper showed PCE of 19%."),
               (8,"Earlier references report PCE of 13%.")]
        rows=text_candidates(pages,("PCE","Voc"),set())
        self.assertEqual({(r["metric"],r["page"],r["value"],r["unit"],r["raw_unit"]) for r in rows},
                         {("PCE",7,"0.55","%","%"),("Voc",7,"0.20398","V","mV")})

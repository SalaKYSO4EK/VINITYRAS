"""Regression checks for table captions, explicit scan labels and scope of illumination."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model_bundle"))
from article_analyzer.jv_methods import apply_illumination_methods, illumination_methods
from article_analyzer.pv_checks import table_context
from article_analyzer.pv_table_prose import apply_table_prose, restore_caption_spacing, table_prose


def cell(name, caption, direction=""):
    return {"page": 5, "table": name, "condition": direction, "sample": "Pristine", "label_origin": "printed_row",
            "sources": {"caption": {"text": caption, "bbox": [1, 2, 3, 4]}, "sample": {"text": "Pristine"},
                        "direction": {"text": direction, "bbox": [1, 3, 4, 5]} if direction else None}}


class JvTableContextTest(unittest.TestCase):
    def test_whole_caption_crosscheck_recovers_aggregation_and_coordinates(self):
        original = "TABLE 2 | Summary of photovoltaic performance for champion devices."
        packed = original.replace("for champion", "forchampion")
        data = cell("TABLE 2", packed, "FB–SC")
        restore_caption_spacing([data], {5: original})
        self.assertEqual(data["sources"]["caption"]["layout_text"], packed)
        context = table_context(data)
        self.assertEqual(context["aggregation"]["value"], "champion")
        self.assertEqual(context["aggregation"]["source"]["text"], original)
        self.assertEqual(context["aggregation"]["source"]["bbox"], [1, 2, 3, 4])
        self.assertEqual(context["scan_direction"], "reverse")
        self.assertEqual(context["scan_direction_source"]["page"], 5)
        other = cell("TABLE 2", packed)
        restore_caption_spacing([other], {4: original, 5: original.replace("champion", "different")})
        self.assertNotIn("layout_text", other["sources"]["caption"])
        self.assertNotIn("aggregation", table_context(other))

    def test_scan_statement_names_its_table_and_conflicts_stay_unresolved(self):
        statement = ("All J–V characteristics were measured under simulated AM 1.5G illumination "
                     "with a reverse scan rate of 0.15 V s−1.Asshownin Figure 5B and Table 1,")
        pages = {3: statement, 5: "TABLE 1 | Photovoltaic parameters.\nTABLE 2 | Champion devices."}
        def direction(table, protocol, printed=""):
            row = cell(table, table + " | Photovoltaic parameters.", printed)
            row["context"] = table_context(row)
            apply_table_prose(row, protocol)
            return row["context"]
        settings = table_prose(pages)
        reverse = direction("TABLE 1", settings)
        self.assertEqual(reverse["scan_direction"], "reverse")
        self.assertEqual(reverse["scan_direction_source"]["page"], 3)
        self.assertIn("Table 1", reverse["scan_direction_source"]["text"])
        self.assertEqual(direction("TABLE 2", settings)["scan_direction"], "")
        self.assertEqual(direction("TABLE 2", settings, "SC–FB")["scan_direction"], "forward")
        conflicting = table_prose({**pages, 4: statement.replace("reverse", "forward")})
        self.assertIn("scan_direction", direction("TABLE 1", conflicting)["ambiguities"])

    def test_reverse_protocol_cannot_supply_forward_illumination(self):
        pages = {2: ("Characterization\nCurrent density–voltage (J–V) curves of solar cells were measured "
                     "under simulated AM 1.5G illumination of 100 mW cm−2 with a reverse scan rate of 0.15 V s−1.")}
        methods = illumination_methods(None, pages=pages)
        self.assertEqual(len(methods), 1)
        for raw, direction in (("FB–SC", "reverse"), ("SC–FB", "forward")):
            context = table_context(cell("TABLE 2", "TABLE 2 | Performance for champion devices.", raw))
            apply_illumination_methods(context, methods, {"table": "TABLE 2"})
            self.assertEqual(context["scan_direction"], direction)
            if direction == "reverse":
                self.assertEqual(context["pin"]["value"], "100")
                self.assertEqual(context["pin"]["source"]["page"], 2)
            else:
                self.assertNotIn("pin", context)
                self.assertNotIn("spectrum", context)
                self.assertEqual(context["illumination_link"]["status"], "needs_review")
                alt = context["ambiguities"]["pin"]["alternatives"][0]
                self.assertEqual(alt["directions"], ["reverse"])


if __name__ == "__main__":
    unittest.main()

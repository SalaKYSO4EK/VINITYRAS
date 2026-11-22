from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import evaluate_rag as audit


def make_tables(descriptions, prefix):
    tables = {t: [] for t in audit.TABLES}
    tables["publications"].append({"publication_id":"D001"})
    for i, (dataset, model, number, table) in enumerate(descriptions, 1):
        uid = f"{prefix}{i}"
        def add(name, data):
            backing = dict.fromkeys(audit.REQUIRED[name], "")
            tables[name].append(dict(backing, publication_id="D001", **data))
        add("tasks", {"task_id":uid, "task_type":"QA"})
        add("datasets", {"dataset_id":uid, "dataset_name":dataset})
        add("configurations", {"config_id":uid, "rag_variant":"A", "generator_model":model})
        add("results", {"result_id":uid,"config_id":uid,"dataset_id":uid,"task_id":uid,
                        "source_page":"2","source_table":table,"metric_name":"EM","metric_value":number,
                        "metric_unit":"fraction","extraction_status":"extracted"})
    return tables


class RagEvaluationTests(unittest.TestCase):
    def score(self, gold, predictions):
        return audit.evaluate(make_tables(predictions,"pred"),make_tables(gold,"gold"),["D001"])

    def test_resolves_semantics_across_ids_and_preserves_multiplicity(self):
        row = ("Corpus","MiniLM","0.32","Table 1")
        score = self.score([row,row],[row,row,row])
        self.assertEqual(score["micro_and_macro"]["strict"]["matched"],2)
        self.assertEqual(score["micro_and_macro"]["strict"]["false_positive"],1)
        self.assertEqual(score["micro_and_macro"]["task_types"]["expected"],1)
        self.assertEqual(score["micro_and_macro"]["task_types"]["matched"],1)

    def test_equal_value_on_wrong_model_or_dataset_is_only_a_weak_match(self):
        gold=[("A","Model1","0.21","Table 1"),("B","Model1","0.32","Table 1")]
        pred=[("A","Model2","0.21","Table 1"),("A","Model1","0.32","Table 1")]
        score=self.score(gold,pred)
        self.assertEqual(score["micro_and_macro"]["strict"]["matched"],0)
        self.assertEqual(score["micro_and_macro"]["number_and_page"]["matched"],2)
        self.assertEqual([e["fields"] for e in score["error_examples"] if e["side"]=="prediction"],
                         [["configuration"],["dataset"]])

    def test_same_value_ambiguous_across_tables_is_not_credited(self):
        gold=[("X","M",".30","Table 1"),("X","M","0.30","Table 2")]
        weak=("X","M","0.300","")
        for pred in ([weak,gold[1]], [gold[1],weak]):
            score=self.score(gold,pred)
            self.assertEqual(score["micro_and_macro"]["strict"]["matched"],1)
            self.assertEqual(score["micro_and_macro"]["number_and_page"]["matched"],2)
            self.assertEqual(score["error_counts"]["ambiguous_gold_candidates"],1)

    def test_false_value_is_not_a_match_and_extra_prediction_costs_precision(self):
        gold=[("Corpus","Model",".34","Table 1")]
        score=self.score(gold,[("Corpus","Model",".43","Table 1")])
        self.assertEqual(score["micro_and_macro"]["strict"]["precision"],0)
        self.assertEqual(score["micro_and_macro"]["number_and_page"]["recall"],0)
        self.assertEqual(score["error_counts"]["different_value"],1)

    def test_right_value_on_another_physical_page_does_not_match(self):
        rows = [("Corpus","Model","0.74","Table 2")]
        gold = make_tables(rows, "original")
        prediction = make_tables(rows, "new")
        prediction["results"][0]["source_page"] = "3"
        score = audit.evaluate(prediction, gold, ["D001"])
        self.assertEqual(score["micro_and_macro"]["number_and_page"]["matched"], 0)
        self.assertEqual(score["error_counts"]["different_page"], 1)

    def test_missing_dataset_is_unrateable_even_if_both_sides_are_blank(self):
        row=("", "Model", ".81", "Table 3")
        score=self.score([row],[row])
        self.assertEqual(score["micro_and_macro"]["strict"]["matched"],0)
        self.assertEqual(score["publications"]["D001"]["strict_key_available_gold"],0)
        missing_only=self.score([row],[])
        self.assertIsNone(missing_only["micro_and_macro"]["strict"]["precision"])
        self.assertEqual(missing_only["micro_and_macro"]["strict"]["recall"],0)


if __name__=="__main__":
    unittest.main()

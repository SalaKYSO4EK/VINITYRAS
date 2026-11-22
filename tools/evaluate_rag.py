"""Evaluate an existing RAG export, using publication-scoped semantic keys rather than local IDs."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import sys
import unicodedata


SPLITS = {
    "train": [f"D{i:03}" for i in range(1, 15)],
    "validation": [f"D{i:03}" for i in range(15, 18)],
    "holdout": [f"D{i:03}" for i in range(18, 22)],
    "test": [f"P{i:03}" for i in range(1, 91)],
}
TABLES = ("publications", "configurations", "datasets", "tasks", "experiments", "results", "issues")
CONFIG = ("rag_variant", "retrieval_method", "retriever_model", "embedding_model", "embedding_version",
          "chunking_method", "chunk_size", "chunk_overlap", "chunk_unit", "index_type", "vector_store",
          "top_k", "reranker_model", "generator_model", "generator_version", "prompt_strategy", "context_strategy")
DATASET = ("dataset_name", "dataset_version", "data_split")
REQUIRED = {
    "publications": ("publication_id",), "configurations": ("config_id", *CONFIG),
    "datasets": ("dataset_id", *DATASET), "tasks": ("task_id", "task_type"),
    "experiments": ("experiment_id",), "issues": ("issue_id",),
    "results": ("result_id", "config_id", "dataset_id", "task_id", "metric_name", "metric_k",
                "metric_value", "metric_unit", "normalized_value", "normalized_unit", "source_page",
                "extraction_status", "source_table", "source_figure", "metric_variant", "aggregation"),
}


def label(text):
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return " ".join(normalized.translate(str.maketrans("–‑‐−", "----")).split())


def number(text):
    try:
        value = Decimal(text.strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def read_export(folder, selected):
    tables = {}
    for table in TABLES:
        with (folder / f"{table}.csv").open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            missing = {"publication_id", *REQUIRED[table]} - set(reader.fieldnames or ())
            if missing:
                raise ValueError(f"{folder}/{table}.csv: missing columns {sorted(missing)}")
            rows = []
            for r in reader:
                if r["publication_id"] in selected:
                    if None in r or None in r.values():
                        raise ValueError(f"{folder}/{table}.csv:{reader.line_num}: incorrect field count")
                    rows.append(r)
            tables[table] = rows
    return tables


def entity_index(rows, id_field, fields):
    index = {}
    for r in rows:
        key = (r["publication_id"], r[id_field])
        if key in index:
            raise ValueError(f"Duplicate {id_field}: {key}")
        index[key] = tuple(label(r[f]) for f in fields)
    return index


@dataclass
class Observation:
    result_id: str
    page: str
    value: Decimal
    semantic: tuple
    # Used only to disambiguate multiple rows with identical main keys.
    qualifiers: tuple
    missing: tuple

    def strict_key(self):
        return self.page, self.value, self.semantic

    def weak_key(self):
        return self.page, self.value


def observations(tables):
    cfg = entity_index(tables["configurations"], "config_id", CONFIG)
    ds = entity_index(tables["datasets"], "dataset_id", DATASET)
    tasks = entity_index(tables["tasks"], "task_id", ("task_type",))
    results = defaultdict(list)
    excluded = defaultdict(Counter)
    for row in tables["results"]:
        pub = row["publication_id"]
        if row["extraction_status"] != "extracted":
            excluded[pub]["status:" + (row["extraction_status"] or "empty")] += 1
            continue
        raw = row["normalized_value"] or row["metric_value"]
        value = number(raw)
        if value is None:
            excluded[pub]["no_finite_numeric_value"] += 1
            continue
        page = row["source_page"].strip()
        dataset = ds.get((pub, row["dataset_id"]), ())
        config = cfg.get((pub, row["config_id"]), ())
        metric = label(row["metric_name"])
        unit = label(row["normalized_unit"] if row["normalized_value"] else row["metric_unit"])
        missing = tuple(name for name, ok in (("source_page", page.isascii() and page.isdecimal() and int(page)>0),
                                              ("metric_name", metric), ("dataset", dataset and dataset[0]),
                                              ("configuration", any(config))) if not ok)
        semantic = (metric, label(row["metric_k"]), unit, dataset, config)
        qualifiers = tuple(label(row[f]) for f in ("source_table", "source_figure", "metric_variant", "aggregation"))
        qualifiers += tasks.get((pub, row["task_id"]), ("",))
        results[pub].append(Observation(row["result_id"], page, value, semantic, qualifiers, missing))
    return results, excluded


def counts(tp, actual, expected):
    return {"matched": tp, "predicted": actual, "expected": expected,
            "false_positive": actual-tp, "missed": expected-tp,
            "precision": tp/actual if actual else None, "recall": tp/expected if expected else None}


def task_types(tables):
    out = defaultdict(set)
    for row in tables["tasks"]:
        if row["task_type"].strip():
            out[row["publication_id"]].add(label(row["task_type"]))
    return out


def compare(expected, actual):
    """One-to-one matching within one publication, retaining multiplicity and unresolved ambiguity."""
    gold = defaultdict(list)
    predictions = defaultdict(list)
    by_number = defaultdict(list)
    by_context = defaultdict(list)
    by_semantic_value = defaultdict(list)
    for i, entry in enumerate(expected):
        by_number[entry.weak_key()].append(i)
        by_context[(entry.page, entry.semantic)].append(i)
        by_semantic_value[(entry.semantic, entry.value)].append(i)
        if not entry.missing:
            gold[entry.strict_key()].append(i)
    for i, entry in enumerate(actual):
        if not entry.missing:
            predictions[entry.strict_key()].append(i)
    matched_gold, matched_actual, ambiguous = set(), set(), set()
    for key, indexes in predictions.items():
        bucket = gold.get(key, ())
        available = defaultdict(deque)
        for j in bucket:
            available[expected[j].qualifiers].append(j)
        for i in indexes:
            target = available.get(actual[i].qualifiers)
            if target:
                matched_gold.add(target.popleft())
                matched_actual.add(i)
        for i in indexes:
            if i in matched_actual:
                continue
            # Missing optional qualifiers may be compatible; an explicit contradiction never is.
            candidates = [ids for qual, ids in available.items() if
                          all(not a or not b or a == b for a, b in zip(actual[i].qualifiers, qual))]
            if len(candidates) == 1 and candidates[0]:
                matched_gold.add(candidates[0].popleft())
                matched_actual.add(i)
            elif len(candidates) > 1:
                ambiguous.add(i)
    weak_gold = Counter(r.weak_key() for r in expected if "source_page" not in r.missing)
    weak_pred = Counter(r.weak_key() for r in actual if "source_page" not in r.missing)
    issues = []
    names = ("metric_name", "metric_k", "unit", "dataset", "configuration")
    for i, row in enumerate(actual):
        if i in matched_actual:
            continue
        candidates = by_number.get(row.weak_key(), [])
        if row.missing:
            cause, fields = "missing_semantic_key", list(row.missing)
        elif i in ambiguous:
            cause, fields = "ambiguous_gold_candidates", ["table/figure/variant/aggregation/task"]
        elif candidates:
            best = min(candidates, key=lambda j: sum(a != b for a, b in zip(row.semantic, expected[j].semantic)))
            fields = [n for n, a, b in zip(names, row.semantic, expected[best].semantic) if a != b]
            if fields:
                cause = "different_semantics"
            else:
                qualifier_names = ("source_table", "source_figure", "metric_variant", "aggregation", "task")
                fields = [n for n, a, b in zip(qualifier_names, row.qualifiers, expected[best].qualifiers)
                          if a and b and a != b]
                cause = "conflicting_qualifier" if fields else "excess_duplicate"
        else:
            candidates = by_context.get((row.page, row.semantic), [])
            if candidates:
                cause, fields = "different_value", ["numeric_value"]
            else:
                candidates = by_semantic_value.get((row.semantic, row.value), [])
                cause, fields = ("different_page", ["source_page"]) if candidates else ("unmatched", [])
        issues.append({"side":"prediction", "result_id":row.result_id, "reason":cause, "fields":fields,
                       "page":row.page, "value":str(row.value),
                       "candidate_gold_ids":[expected[j].result_id for j in candidates[:5]]})
    for i, row in enumerate(expected):
        if i not in matched_gold:
            issues.append({"side":"gold","result_id":row.result_id,"reason":"missed",
                           "page":row.page,"value":str(row.value),"missing_key_fields":list(row.missing)})
    return (counts(len(matched_gold),len(actual),len(expected)),
            counts(sum((weak_gold & weak_pred).values()),len(actual),len(expected)),issues)


def evaluate(pred, gold, selected, limit=30, errors_writer=None):
    outputs, excluded_pred = observations(pred)
    targets, excluded_gold = observations(gold)
    task_pred,task_gold = task_types(pred),task_types(gold)
    papers = {}
    evidence = []
    errors = Counter()
    totals = {metric:[0,0,0] for metric in ("strict","number_and_page","task_types")}
    for pid in selected:
        strict,weak,issues = compare(targets[pid],outputs[pid])
        task = counts(len(task_pred[pid]&task_gold[pid]),len(task_pred[pid]),len(task_gold[pid]))
        papers[pid]={"strict":strict,"number_and_page":weak,"task_types":task,
                     "strict_key_available_gold":sum(not r.missing for r in targets[pid]),
                     "strict_key_available_predictions":sum(not r.missing for r in outputs[pid]),
                     "excluded_gold":dict(excluded_gold[pid]),"excluded_predictions":dict(excluded_pred[pid]),
                     "task_types_missed":sorted(task_gold[pid]-task_pred[pid]),
                     "task_types_unexpected":sorted(task_pred[pid]-task_gold[pid])}
        for metric in totals:
            for i, n in enumerate(("matched","predicted","expected")):
                totals[metric][i]+=papers[pid][metric][n]
        for issue in issues:
            issue={"publication_id":pid,**issue}
            errors[issue["reason"]]+=1
            if len(evidence)<limit:
                evidence.append(issue)
            if errors_writer:
                errors_writer.write(json.dumps(issue,ensure_ascii=False)+"\n")
    totals={name:counts(*values) for name,values in totals.items()}
    for metric in totals:
        for stat in ("recall","precision"):
            values=[p[metric][stat] for p in papers.values() if p[metric][stat] is not None]
            totals[metric]["macro_"+stat]=sum(values)/len(values) if values else None
            totals[metric]["macro_"+stat+"_publications"]=len(values)
    return {"micro_and_macro":totals,"publications":papers,"error_counts":dict(errors),"error_examples":evidence}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predictions",required=True,type=Path)
    parser.add_argument("--gold",type=Path,help="Open dev_gold directory, evaluated only after extraction.")
    parser.add_argument("--split",required=True,choices=SPLITS)
    parser.add_argument("--publications",nargs="+",help="Explicit evaluated subset of the split; default is all of it.")
    parser.add_argument("--report",required=True,type=Path)
    parser.add_argument("--errors-jsonl",type=Path)
    parser.add_argument("--error-limit",type=int,default=30)
    args=parser.parse_args()
    selected=args.publications or SPLITS[args.split]
    if len(set(selected))!=len(selected) or not set(selected)<=set(SPLITS[args.split]):
        parser.error("publication IDs must be unique members of the specified split")
    if args.error_limit<0:
        parser.error("--error-limit must be nonnegative")
    if args.split=="test" and args.gold:
        parser.error("P001–P090 have no public gold; do not use dev_gold for test")
    if args.split!="test" and not args.gold:
        parser.error("--gold is required for a labeled split")
    destinations=[p.resolve() for p in (args.report,args.errors_jsonl) if p]
    if len(set(destinations))!=len(destinations) or any(args.gold and p.is_relative_to(args.gold.resolve()) for p in destinations):
        parser.error("reports must be distinct paths outside dev_gold")
    if any(p==args.predictions.resolve()/(t+".csv") for p in destinations for t in TABLES):
        parser.error("a report cannot replace an input table")
    pred=read_export(args.predictions,set(selected))
    pubs={r["publication_id"] for r in pred["publications"]}
    report={"split":args.split,"selected_publications":selected,
            "missing_publications_in_export":sorted(set(selected)-pubs),
            "prediction_table_counts":{k:len(v) for k,v in pred.items()}}
    if args.split=="test":
        report["evaluation"]="no public gold; coverage and accuracy are not measurable"
    else:
        gold=read_export(args.gold,set(selected))
        if {r["publication_id"] for r in gold["publications"]}!=set(selected):
            parser.error("gold publications.csv does not cover the declared scope")
        if args.errors_jsonl:
            with args.errors_jsonl.open("w",encoding="utf-8") as stream:
                report.update(evaluate(pred,gold,selected,args.error_limit,stream))
        else:
            report.update(evaluate(pred,gold,selected,args.error_limit))
        report["scope"]="Extracted finite normalized/metric values; exact Decimal. Both ratios count occurrences. Strict = publication, physical page, normalized unit, metric name/k, dataset name/version/split and structured configuration fields excluding note/IDs. When these collide, printed table/figure, variant, aggregation and task disambiguate. Weak = publication, physical page and number. Task coverage uses distinct explicit task types per publication. Does not verify PDF evidence or measure quality of nonnumeric entities."
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({k:report[k] for k in ("split","prediction_table_counts","micro_and_macro","evaluation") if k in report},ensure_ascii=False))


if __name__=="__main__":
    try:
        main()
    except (OSError,ValueError,csv.Error) as exc:
        sys.exit(str(exc))

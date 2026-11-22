"""Local LoRA experiment for finding a requested numeric cell in a scientific RAG table row."""
import argparse
import csv
from collections import Counter, defaultdict, deque
from datetime import datetime
import hashlib
import json
from pathlib import Path
import random
import re
import sys
import time
import unicodedata

sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_rag_titles import GROUPS, LocalNuExtract, ExtractionError, ROOT, save_json

import pdfplumber
from peft import LoraConfig, TaskType, get_peft_model
from pdfplumber.utils.exceptions import PdfminerException
import torch

from article_analyzer.rag_tables import extract_table_cells


TEMPLATE = {"result": {"raw_value": ""}}
LABELS = ("sample", "dataset", "generator", "metric")
HEADERS = ("Method", "Dataset", "Generator", "Metric")
RESERVED_TABLE = ("D007", 7, "Table 2")
SEED = 53
MAX_OUTPUT = 42
METRIC_ALIASES = {"acc": "accuracy", "step": "retrievalandgenerationstepsperquery",
                  "steps": "retrievalandgenerationstepsperquery", "time": "relativetimeperquery"}
DATASET_ALIASES = {"2wiki": "2wikimultihopqa", "msmacro": "msmarco", "macroaverageover12ragbenchmarks": "",
                   "combinedsixqabenchmarks": ""}


def compact(value):
    return re.sub(r"[^a-z0-9.]+", "", unicodedata.normalize("NFKC", value).casefold())


def exact_source(value):
    return "".join(unicodedata.normalize("NFC", value).split())


def compare_identity(sample, dataset, generator, metric):
    sample, dataset, generator, metric = map(compact, (sample, dataset, generator, metric))
    return ({"adaptiveragours": "adaptiverag"}.get(sample, sample), DATASET_ALIASES.get(dataset, dataset),
            {"gpt3.5turboinstruct": "gpt3.5turbo"}.get(generator, generator), METRIC_ALIASES.get(metric, metric))


def label_group(cell):
    return tuple(cell[label] for label in LABELS)


def query_text(cells, target):
    query = " | ".join(f"{header}: {target[label]}" for header, label in zip(HEADERS, LABELS) if target[label])
    context = ["Printed table columns in order. The last line is the corresponding row of cells."]
    for header, label in zip(HEADERS, LABELS):
        labels = [cell[label] for cell in cells]
        if any(labels):
            context.append(header + " | " + " | ".join(label or "(unlabelled)" for label in labels))
    context.append("Printed values | " + " | ".join(cell["raw"] for cell in cells))
    return ("Copy the exact printed numeric value for the requested method, dataset, generator and metric. "
            "Choose the column matching every requested label. Preserve its suffix if printed.\n"
            f"Requested cell | {query}\n" + "\n".join(context))


def rows_from_page(page, page_number):
    rows = defaultdict(list)
    for cell in extract_table_cells(page, page_number):
        rows[cell["table"], tuple(cell["row_bbox"])].append(cell)
    return rows


def verified_cells(page, cells):
    cells = sorted(cells, key=lambda cell: cell["bbox"][0])
    verified = []
    for cell in cells:
        left, top, right, bottom = cell["bbox"]
        text = page.crop((left - .2, top - .2, right + .2, bottom + .2)).extract_text(x_tolerance=1.1) or ""
        if exact_source(text) == exact_source(cell["raw"]):
            verified.append(cell)
    # Keep the entire aligned row that the parser could verify. Do not introduce
    # a compact row with unknown cells removed between the verified values.
    return verified if len(verified) == len(cells) else []


def sample_rows(corpus, ids, *, only_reserved=False, exclude_reserved=False):
    by_article, inventory = {}, {}
    for publication in ids:
        filename = corpus / "dev" / f"{publication}.pdf"
        start = time.perf_counter()
        counts = Counter()
        candidates = []
        with filename.open("rb") as stream:
            source_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
        with pdfplumber.open(filename) as pdf:
            for number, page in enumerate(pdf.pages, 1):
                counts["pages"] += 1
                if only_reserved and (publication, number) != RESERVED_TABLE[:2]:
                    continue
                try:
                    if not page.chars:
                        counts["no_text_pages"] += 1
                        continue
                    tables = rows_from_page(page, number)
                except (PdfminerException, OSError, ValueError) as error:
                    counts["unreadable_layout_pages"] += 1
                    print(publication, number, type(error).__name__, flush=True)
                    continue
                if not tables:
                    counts["pages_without_supported_tables"] += 1
                for (table, _), cells in tables.items():
                    is_reserved = (publication, number, table) == RESERVED_TABLE
                    if (only_reserved and not is_reserved) or (exclude_reserved and is_reserved):
                        continue
                    counts["detected_rows"] += 1
                    stable_values = verified_cells(page, cells)
                    if len(stable_values) < 3 or len({exact_source(row["raw"]) for row in stable_values}) <= 1:
                        counts["rows_without_three_verified_alternatives"] += 1
                        continue
                    row_id = f"{publication}:{number}:{table}:{cells[0]['row_bbox'][1]:.2f}"
                    for index, cell in enumerate(stable_values):
                        requested = [component for component, field in enumerate(LABELS) if cell[field]]
                        matches = [candidate for candidate in stable_values if
                                   all(label_group(candidate)[component] == label_group(cell)[component]
                                       for component in requested)]
                        if len(matches) != 1 or not cell["metric"] or cell.get("generator_note"):
                            counts["ambiguous_target_cells"] += 1
                            continue
                        if not cell["dataset"] and not cell.get("aggregation") and any(row["dataset"] for row in stable_values):
                            counts["ambiguous_dataset_cells"] += 1
                            continue
                        candidates.append({
                            "case_id": f"{row_id}:{index + 1}", "row_id": row_id, "publication_id": publication,
                            "source_file": filename.name, "source_sha256": source_sha256, "page": number, "table": table,
                            "candidate_count": len(cells), "prompt": query_text(stable_values, cell),
                            "source_evidence": cell, "source_row_values": [candidate["raw"] for candidate in stable_values],
                        })
        by_article[publication] = candidates
        inventory[publication] = {**counts, "eligible_cells": len(candidates),
                                  "eligible_rows": len({case["row_id"] for case in candidates}),
                                  "seconds": round(time.perf_counter() - start, 3)}
        print("source", publication, inventory[publication], flush=True)
    return by_article, inventory


def filtered_csv(path, publications):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return [row for row in csv.DictReader(stream) if row["publication_id"] in publications]


def references(corpus, publications):
    root = corpus / "dev_gold"
    configurations = {(r["publication_id"], r["config_id"]): r for r in
                      filtered_csv(root / "configurations.csv", publications)}
    datasets = {(r["publication_id"], r["dataset_id"]): r["dataset_name"] for r in
                filtered_csv(root / "datasets.csv", publications)}
    mapped = defaultdict(set)
    for row in filtered_csv(root / "results.csv", publications):
        pid = row["publication_id"]
        conf = configurations.get((pid, row["config_id"]), {})
        if row["source_table"] and row["source_page"] and row["raw_value"] and conf:
            method, dataset, generator, metric = compare_identity(
                conf["rag_variant"], datasets.get((pid, row["dataset_id"]), ""), conf["generator_model"],
                row["metric_name"],
            )
            mapped[pid, int(row["source_page"]), row["source_table"], method, dataset, metric].add(
                (generator, row["raw_value"]))
    return mapped


def gold_answer(case, reference):
    method, dataset, generator, metric = compare_identity(*label_group(case["source_evidence"]))
    choices = reference.get((case["publication_id"], case["page"], case["table"], method, dataset, metric), set())
    selected = {value for name, value in choices if name == generator}
    if not selected and len({value for _, value in choices}) == 1:
        selected = {value for _, value in choices}
    if len(selected) != 1:
        return None
    return next(iter(selected))


def choose_cases(by_article, cap, per_table, seed, reference=None):
    rng = random.Random(seed)
    chosen, inventory = [], {}
    for publication, candidates in by_article.items():
        tables = defaultdict(lambda: defaultdict(list))
        unmatched = 0
        for case in candidates:
            if reference is not None:
                label = gold_answer(case, reference)
                if label is None or exact_source(label) != exact_source(case["source_evidence"]["raw"]):
                    unmatched += 1
                    continue
                case = {**case, "training_target": label}
            tables[case["table"]][case["row_id"]].append(case)
        randomized_tables = list(tables)
        rng.shuffle(randomized_tables)
        ordered = deque()
        for table in randomized_tables:
            rows = list(tables[table].values())
            rng.shuffle(rows)
            ordered.append(deque(rows[:per_table]))
        selected = 0
        used_metrics = Counter()
        while ordered and selected < cap:
            possible = ordered.popleft()
            row = possible.popleft()
            rng.shuffle(row)
            case = min(row, key=lambda entry: used_metrics[entry["source_evidence"]["metric"].casefold()])
            used_metrics[case["source_evidence"]["metric"].casefold()] += 1
            chosen.append(case)
            selected += 1
            if possible:
                ordered.append(possible)
        inventory[publication] = {"selected": selected, "gold_without_unique_literal_alignment": unmatched}
    return chosen, inventory


def infer(client, cases, filename):
    predictions = []
    start = time.perf_counter()
    for case in cases:
        tick = time.perf_counter()
        prediction, raw, error = "", "", ""
        try:
            candidate = client.extract(case["prompt"], TEMPLATE, max_new_tokens=MAX_OUTPUT)
            prediction = candidate.data["result"]["raw_value"]
            raw = candidate.raw_output
        except (ExtractionError, ValueError) as exc:
            error = str(exc)
            raw = getattr(exc, "raw_output", "")
        predictions.append({"case_id": case["case_id"], "prediction": prediction, "raw_model_output": raw,
                            "error": error, "seconds": round(time.perf_counter() - tick, 3)})
        save_json(filename, predictions)
        print("infer", filename.stem, case["case_id"], predictions[-1]["seconds"], flush=True)
    if not predictions:
        save_json(filename, [])
    return predictions, round(time.perf_counter() - start, 3)


def score(cases, predictions, reference, seconds, filename):
    assert len(cases) == len(predictions)
    results = []
    for case, predicted in zip(cases, predictions):
        assert case["case_id"] == predicted["case_id"]
        expected = case["source_evidence"]["raw"]
        gold = gold_answer(case, reference)
        aligned = gold is not None and exact_source(gold) == exact_source(expected)
        exact = exact_source(predicted["prediction"]) == exact_source(expected)
        results.append({**predicted, "source_file": case["source_file"], "page": case["page"], "table": case["table"],
                        "source_evidence": case["source_evidence"], "official_gold": gold,
                        "gold_aligned": aligned, "exact_printed_value": exact,
                        "source_and_gold_confirmed": aligned and exact})
    result = {
        "cases": len(results), "documents": sorted({case["publication_id"] for case in cases}),
        "exact_pdf": sum(row["exact_printed_value"] for row in results),
        "gold_aligned": sum(row["gold_aligned"] for row in results),
        "exact_pdf_and_gold": sum(row["source_and_gold_confirmed"] for row in results),
        "invalid_response": sum(bool(row["error"]) for row in results),
        "generation_seconds": seconds, "results": results,
    }
    save_json(filename, result)
    return {name: value for name, value in result.items() if name != "results"}


def train_epoch(client, model, optimizer, cases, epoch, report, out):
    model.train()
    model.config.use_cache = False
    cases = list(cases)
    random.Random(SEED + epoch).shuffle(cases)
    for case in cases:
        started = time.perf_counter()
        prompt = client.tokenizer.encode(client.prompt(case["prompt"], TEMPLATE), add_special_tokens=False)
        reply = json.dumps({"result": {"raw_value": case["training_target"]}}, ensure_ascii=False,
                           separators=(",", ":"))
        labels = client.tokenizer.encode(reply, add_special_tokens=False) + [client.tokenizer.eos_token_id]
        optimizer.zero_grad(set_to_none=True)
        loss = model(input_ids=torch.tensor([prompt + labels]), labels=torch.tensor([[-100] * len(prompt) + labels])).loss
        if not torch.isfinite(loss):
            raise ValueError("Non-finite loss")
        loss.backward()
        optimizer.step()
        report["steps"].append({"epoch": epoch, "case_id": case["case_id"], "loss": round(loss.item(), 6),
                                "seconds": round(time.perf_counter() - started, 3), "tokens": len(prompt) + len(labels)})
        save_json(out / "run.json", report)
        print("train", report["steps"][-1], flush=True)
    model.config.use_cache = True
    model.eval()
    model.save_pretrained(out / f"epoch-{epoch}", safe_serialization=True)


def main(corpus, out, threads, epochs, train_cap, eval_cap):
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"Select a new empty output folder: {out}")
    out.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    torch.manual_seed(SEED)
    groups = {name: [f"D{index:03}" for index in members] for name, members in GROUPS.items()}
    report = {
        "started": datetime.now().astimezone().isoformat(), "split": groups, "seed": SEED, "threads": threads,
        "task": "Scientific RAG numeric cell from full aligned source row and multi-level evidence headings",
        "template": TEMPLATE, "max_new_tokens": MAX_OUTPUT, "reserved_development_table": RESERVED_TABLE,
        "reserved_scope": "One whole table excluded from optimization. The same publication can occur in training.",
        "case_selection": {"train_per_article": train_cap, "train_per_table": min(train_cap, 4),
                           "eval_per_article": eval_cap, "eval_per_table": eval_cap, "one_example_per_source_row": True},
        "selection_metric": "exact printed value with official gold alignment; ties prefer base and earlier checkpoints",
        "adapter": {"rank": 8, "alpha": 16, "target_modules": ["q_proj", "v_proj"], "learning_rate": .0002},
        "steps": [], "baseline": {}, "validation": {}, "holdout": {},
    }
    save_json(out / "run.json", report)
    val_source, report["validation_sources"] = sample_rows(corpus, groups["selection"])
    validation, report["validation_sampling"] = choose_cases(val_source, eval_cap, eval_cap, SEED)
    save_json(out / "validation-cases.json", validation)
    client = LocalNuExtract(ROOT / "model_bundle/models/NuExtract-1.5-tiny", threads=threads)
    client.model.set_attn_implementation("sdpa")
    report.update(model_load_seconds=client.load_seconds, base_revision=client.revision)
    base_validation, duration = infer(client, validation, out / "base-validation.raw.json")
    val_reference = references(corpus, groups["selection"])
    report["baseline"] = score(validation, base_validation, val_reference, duration, out / "base-validation.json")
    save_json(out / "run.json", report)

    train_source, report["training_sources"] = sample_rows(corpus, groups["train"], exclude_reserved=True)
    train_reference = references(corpus, groups["train"])
    train, report["training_sampling"] = choose_cases(train_source, train_cap, min(train_cap, 4), SEED, train_reference)
    report["training_count"] = len(train)
    save_json(out / "training-cases.json", train)
    save_json(out / "run.json", report)
    if not train:
        raise ValueError("No official gold training values matched directly verified PDF table cells")
    model = get_peft_model(client.model, LoraConfig(r=8, lora_alpha=16, lora_dropout=.05,
                           target_modules=["q_proj", "v_proj"], bias="none", task_type=TaskType.CAUSAL_LM))
    client.model = model
    report["trainable_parameters"] = sum(p.numel() for p in model.parameters() if p.requires_grad)
    optimizer = torch.optim.AdamW((parameter for parameter in model.parameters() if parameter.requires_grad), lr=.0002)
    best_epoch, best_score = 1, -1
    for epoch in range(1, epochs + 1):
        train_epoch(client, model, optimizer, train, epoch, report, out)
        label = f"epoch-{epoch}"
        output, duration = infer(client, validation, out / f"{label}-validation.raw.json")
        result = score(validation, output, val_reference, duration, out / f"{label}-validation.json")
        report["validation"][label] = result
        if result["exact_pdf_and_gold"] > best_score:
            best_epoch, best_score = epoch, result["exact_pdf_and_gold"]
        save_json(out / "run.json", report)
    performance = report["baseline"]["exact_pdf_and_gold"]
    selected = "base" if performance >= best_score else f"epoch-{best_epoch}"
    adapter_weights = out / f"epoch-{best_epoch}/adapter_model.safetensors"
    report["locked_selection"] = {"selected": selected, "best_adapter": f"epoch-{best_epoch}",
                                  "base_exact": performance, "adapter_exact": best_score,
                                  "adapter_sha256": hashlib.sha256(adapter_weights.read_bytes()).hexdigest(),
                                  "locked_at": datetime.now().astimezone().isoformat()}
    save_json(out / "run.json", report)

    # Read neither the holdout pages nor their labels before committing the selector.
    holdout_source, report["holdout_sources"] = sample_rows(corpus, groups["holdout"])
    holdout, report["holdout_sampling"] = choose_cases(holdout_source, eval_cap, eval_cap, SEED)
    reserved_source, report["reserved_sources"] = sample_rows(corpus, [RESERVED_TABLE[0]], only_reserved=True)
    reserved, report["reserved_sampling"] = choose_cases(reserved_source, eval_cap, eval_cap, SEED)
    save_json(out / "holdout-cases.json", holdout)
    save_json(out / "reserved-table-cases.json", reserved)
    with model.disable_adapter():
        base_holdout = infer(client, holdout, out / "base-holdout.raw.json")
        base_reserved = infer(client, reserved, out / "base-reserved-table.raw.json")
    model.load_adapter(out / f"epoch-{best_epoch}", adapter_name="selected", is_trainable=False)
    model.set_adapter("selected")
    adapter_holdout = infer(client, holdout, out / "adapter-holdout.raw.json")
    adapter_reserved = infer(client, reserved, out / "adapter-reserved-table.raw.json")
    holdout_reference = references(corpus, groups["holdout"])
    for split, cases, reference, generations in (
        ("holdout", holdout, holdout_reference, (base_holdout, adapter_holdout)),
        ("reserved-table", reserved, train_reference, (base_reserved, adapter_reserved)),
    ):
        report[split] = {
            label: score(cases, raw, reference, duration, out / f"{label}-{split}.json")
            for label, (raw, duration) in zip(("base", "adapter"), generations)
        }
    report["training_seconds"] = round(sum(step["seconds"] for step in report["steps"]), 3)
    report["total_seconds"] = round(time.perf_counter() - start, 3)
    save_json(out / "run.json", report)
    print("Done", report["locked_selection"], report["holdout"], report["total_seconds"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--epochs", type=int, default=2)
    parser.add_argument("--train-cap", type=int, default=5)
    parser.add_argument("--eval-cap", type=int, default=8)
    args = parser.parse_args()
    if min(args.threads, args.epochs, args.train_cap, args.eval_cap) < 1:
        parser.error("all count parameters must be positive")
    main(args.corpus.resolve(), args.out.resolve(), args.threads, args.epochs, args.train_cap, args.eval_cap)

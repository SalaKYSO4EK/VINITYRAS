"""Compile user criteria with Qwen and project directly from positioned PDF cells."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from article_analyzer.criteria import InterpretationError, Source, configured_interpreter
from article_analyzer.qwen_api import QwenApiError
from article_analyzer.photovoltaic_tables import compact, detect_pdf
from tools.interpret_criteria import read_source, save_json


def requested_metrics(plan, config):
    aliases = {compact(alias): metric for metric, names in config["criterion_fields"].items() for alias in names}
    fields = [field["name"] for group in plan["extraction"]["groups"] for field in group["fields"]]
    identifiers = [compact(field).rsplit(".", 1)[-1] for field in fields]
    return list(dict.fromkeys(aliases[field] for field in identifiers if field in aliases))


def document(path, metrics):
    started = time.perf_counter()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    t1 = time.perf_counter()
    detected = detect_pdf(path)
    t2 = time.perf_counter()
    records = [r for r in detected["records"] if r["metric"] in metrics]
    return {
        "name": path.name, "sha256": digest, "candidate_regions": detected["candidate_regions"],
        "detected_cells": detected["records"], "records": records,
        "seconds": {"hash": round(t1 - started, 4), "pdf_layout_and_binding": round(t2 - t1, 4),
                    "criteria_projection": round(time.perf_counter() - t2, 6)},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text")
    source.add_argument("--criteria", type=Path)
    parser.add_argument("--pdf", type=Path, action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=4)
    args = parser.parse_args()
    start = time.perf_counter()
    criteria = (read_source(args.criteria, None) if args.criteria else
                Source("request", hashlib.sha256(args.text.encode()).hexdigest(), {1: args.text}))
    try:
        model = configured_interpreter(ROOT, args.threads)
        plan = model.interpret(criteria, 900)
    except QwenApiError as exc:
        save_json(args.out, {"criteria": criteria.text(), "error": str(exc)})
        print(str(exc), file=sys.stderr)
        return 1
    except InterpretationError as exc:
        save_json(args.out, {"criteria": criteria.text(), "qwen_raw_responses": model.raw_responses,
                             "error": str(exc), "raw_error": exc.raw})
        print(str(exc), file=sys.stderr)
        return 1
    config = json.loads((ROOT / "config/solar-pdf-tables.json").read_text(encoding="utf-8"))
    metrics = requested_metrics(plan, config)
    result = {"criteria": criteria.text(), "criteria_plan": plan, "qwen_raw_responses": model.raw_responses,
              "metrics": metrics, "documents": [], "nuextract_calls": 0}
    if not metrics or plan.get("page_errors"):
        result["error"] = "Qwen did not select a supported field from the photovoltaic domain configuration"
        save_json(args.out, result)
        return 1
    for path in args.pdf:
        d = document(path, metrics)
        result["documents"].append(d)
        print(path.name, metrics, len(d["records"]), d["seconds"], flush=True)
        save_json(args.out, result)
    result["wall_seconds"] = round(time.perf_counter() - start, 3)
    save_json(args.out, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

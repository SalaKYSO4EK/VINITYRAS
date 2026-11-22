"""Run title extraction in a process that releases model memory on exit."""
from __future__ import annotations

from pathlib import Path
import json
import sys
import time

from article_analyzer.local_model import LocalNuExtract
from article_analyzer.pdf_lab import ROOT, title_from_model
from tools.interpret_criteria import save_json


def extract_titles(model, output, articles, load_seconds, pages_dir=None):
    output = Path(output)
    report = json.loads(output.read_text(encoding="utf-8")) if output.exists() else {"responses": {}, "errors": []}
    report["load_seconds"] = load_seconds
    for path in map(Path, articles):
        try:
            cached = Path(pages_dir, path.name + ".json") if pages_dir else None
            first_page = json.loads(cached.read_text(encoding="utf-8"))[0] if cached and cached.exists() else None
            report["responses"][path.name] = title_from_model(path, model, first_page)
        except Exception as exc:
            report["errors"].append({"file": path.name, "phase": "nuextract",
                                     "error": f"{type(exc).__name__}: {exc}"})
            report["responses"][path.name] = {"status": "processing_failed", "raw": getattr(exc, "raw_output", "")}
        save_json(output, report)


def main(args):
    output, threads, *articles = args
    pages_dir = None
    if articles[:1] == ["--pages-dir"]:
        _, pages_dir, *articles = articles
    start = time.perf_counter()
    model = LocalNuExtract(ROOT / "models/NuExtract-1.5-tiny", threads=int(threads))
    extract_titles(model, output, articles, round(time.perf_counter() - start, 3), pages_dir)


if __name__ == "__main__":
    main(sys.argv[1:])

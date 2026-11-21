"""Run title extraction in a process that releases model memory on exit."""
from __future__ import annotations

from pathlib import Path
import sys
import time

from article_analyzer.local_model import LocalNuExtract
from article_analyzer.pdf_lab import ROOT, title_from_model
from tools.interpret_criteria import save_json


def main(args):
    output, threads, *articles = args
    start = time.perf_counter()
    model = LocalNuExtract(ROOT / "models/NuExtract-1.5-tiny", threads=int(threads))
    report = {"load_seconds": round(time.perf_counter() - start, 3), "responses": {}, "errors": []}
    for path in map(Path, articles):
        try:
            report["responses"][path.name] = title_from_model(path, model)
        except Exception as exc:
            report["errors"].append({"file": path.name, "phase": "nuextract",
                                     "error": f"{type(exc).__name__}: {exc}"})
            report["responses"][path.name] = {"status": "processing_failed", "raw": getattr(exc, "raw_output", "")}
        save_json(Path(output), report)


if __name__ == "__main__":
    main(sys.argv[1:])

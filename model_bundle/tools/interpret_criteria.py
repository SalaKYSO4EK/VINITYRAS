"""Compile the criteria for one request, optionally extracting facts from an article."""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from article_analyzer.criteria import (
    InterpretationError,
    LocalCriteriaInterpreter,
    Source,
    configured_interpreter,
    exact_model_fields,
    extract_with_profile,
)
from article_analyzer.local_model import ExtractionError, LocalNuExtract
from article_analyzer.qwen_api import QwenApiError
from article_analyzer.schema_config import apply_schema_config


def selected_pages(selection: str | None, count: int) -> list[int]:
    if selection is None:
        return list(range(1, count + 1))
    pages = set()
    for part in selection.split(","):
        ends = part.strip().split("-")
        if len(ends) not in (1, 2) or not all(end.isascii() and end.isdecimal() for end in ends):
            raise ValueError(f"Invalid page range: {part!r}")
        first, last = int(ends[0]), int(ends[-1])
        if not 1 <= first <= last <= count:
            raise ValueError(f"Page range {part!r} outside PDF pages 1–{count}")
        pages.update(range(first, last + 1))
    return sorted(pages)


def read_source(path: Path, selection: str | None) -> Source:
    content = path.read_bytes()
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader

        pdf = PdfReader(io.BytesIO(content))
        pages = {number: (pdf.pages[number - 1].extract_text() or "").strip()
                 for number in selected_pages(selection, len(pdf.pages))}
        empty = [number for number, value in pages.items() if not value]
        if empty:
            raise ValueError(f"No extractable text on PDF page(s) {empty} in {path}. Supply OCR text or select text pages.")
    else:
        if selection is not None:
            raise ValueError("Page ranges apply only to PDF inputs")
        pages = {1: content.decode("utf-8-sig").strip()}
    if not any(pages.values()):
        raise ValueError(f"No text in {path}")
    return Source(path.name, hashlib.sha256(content).hexdigest(), pages)


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
        json.dump(data, temporary, ensure_ascii=False, indent=2, allow_nan=False)
        temporary.write("\n")
    temporary_path = Path(temporary.name)
    try:
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--criteria", type=Path, help="Criteria PDF or UTF-8 text file")
    source.add_argument("--text", help="Criteria as text")
    parser.add_argument("--criteria-pages", help="PDF page range, e.g. 3-6 or 3,5,7-9")
    parser.add_argument("--article", type=Path, help="Optional article PDF or UTF-8 text file")
    parser.add_argument("--article-pages", help="PDF page range for article evidence")
    parser.add_argument("--group", action="append", help="Extract only the named group (repeatable)")
    parser.add_argument("--exact-fields", action="store_true", help="Send only the requested value fields to NuExtract")
    parser.add_argument("--schema-config", type=Path, help="Optional subject configuration for submission tables")
    parser.add_argument("--out", type=Path, help="Save JSON to this path instead of printing it")
    parser.add_argument("--interpreter-model", type=Path, help="Explicit local GGUF override")
    parser.add_argument("--llama-completion", type=Path, help="Explicit local llama.cpp executable")
    parser.add_argument("--adapter", type=Path, help="Optional NuExtract adapter")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--criteria-tokens", type=int, default=3072)
    parser.add_argument("--extract-tokens", type=int, default=700)
    args = parser.parse_args()
    if args.text is not None and (args.criteria_pages or not args.text.strip()):
        parser.error("--text must be non-empty and cannot be used with --criteria-pages")
    if not args.article and (args.article_pages or args.group or args.adapter):
        parser.error("--article-pages, --group and --adapter require --article")
    if args.out and any(args.out.resolve() == path.resolve()
                        for path in (args.criteria, args.article) if path is not None):
        parser.error("--out must differ from the input files")

    result = {}
    exit_code = 0
    try:
        criteria = (read_source(args.criteria, args.criteria_pages) if args.criteria else
                    Source("request", hashlib.sha256(args.text.encode("utf-8")).hexdigest(), {1: args.text.strip()}))
        article = read_source(args.article, args.article_pages) if args.article else None
        if args.interpreter_model or args.llama_completion:
            interpreter = LocalCriteriaInterpreter(
                args.interpreter_model or ROOT / "models/criteria/qwen2.5-1.5b-instruct-q4_k_m.gguf",
                args.llama_completion or ROOT / "models/criteria/llama-b8157/llama-completion.exe", args.threads)
        else:
            interpreter = configured_interpreter(ROOT, args.threads)
        print(f"Interpreting criteria with {interpreter.model_name} ({interpreter.runtime})…",
              file=sys.stderr, flush=True)
        result = interpreter.interpret(criteria, args.criteria_tokens)
        if args.exact_fields:
            exact_model_fields(result)
        if args.schema_config:
            result = apply_schema_config(result, args.schema_config)
        del interpreter
        print("Criteria JSON compiled.", file=sys.stderr, flush=True)
        if result.get("page_errors"):
            result["error"] = "Some criteria pages could not be interpreted. See page_errors."
            exit_code = 1
        elif article:
            print("Extracting article facts with NuExtract…", file=sys.stderr, flush=True)
            client = LocalNuExtract(ROOT / "models/NuExtract-1.5-tiny", adapter_path=args.adapter,
                                   threads=args.threads)
            client.model.set_attn_implementation("sdpa")
            result["article_extraction"] = extract_with_profile(client, result, article,
                                                               args.group, args.extract_tokens)
            if any("error" in group for group in result["article_extraction"]["groups"].values()):
                result["error"] = "One or more extraction groups returned invalid JSON. See article_extraction.groups."
                exit_code = 1
    except (OSError, ValueError, QwenApiError) as error:
        result["error"] = str(error)
        if isinstance(error, InterpretationError):
            result["raw_interpretation"] = error.raw
        if isinstance(error, ExtractionError):
            result["raw_extraction"] = error.raw_output
        exit_code = 1
    if args.out:
        save_json(args.out, result)
        print(f"Saved {args.out}", file=sys.stderr)
    else:
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    if exit_code:
        print(result["error"], file=sys.stderr)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""Apply the frozen RAG-title LoRA to first pages. No reference tables are read."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "model_bundle"))
import pdfplumber
from article_analyzer.local_model import ExtractionError, LocalNuExtract
from article_analyzer.pdf_title_evidence import printed_metadata_title
from article_analyzer.rag_documents import first_heading, flat
from article_analyzer.visible_pdf import visible_page
from tools.interpret_criteria import save_json

TEMPLATE = {"publications": [{"title": ""}]}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def signed_hash(value):
    data = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def cached(file, key):
    try:
        item = json.loads(file.read_text(encoding="utf-8"))
        signature = item.pop("record_sha256")
        if (item["inference_key"] == key and signature == signed_hash(item)
                and item["status"] != "processing_failed"):
            return item
    except (OSError, KeyError, ValueError, TypeError):
        pass
    return None


def prior_evaluations(adapter, hashes, revision, threads):
    """Reuse raw generations from the training run only with a bound checksum manifest."""
    folder = adapter.parent
    manifest = folder / "prediction-provenance.json"
    if not manifest.is_file():
        return {}
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    if (saved["adapter_sha256"] != hashes["adapter_model.safetensors"] or saved["threads"] != threads
            or saved["base_revision"] != revision or saved["epoch"] != adapter.name):
        return {}
    if any(digest(folder/name) != value for name, value in saved["files"].items()):
        return {}
    inputs = json.loads((folder / "inputs.json").read_text(encoding="utf-8"))
    rows = {}
    for filename in saved["raw_files"]:
        for item in json.loads((folder/filename).read_text(encoding="utf-8")):
            doc = inputs[item["publication_id"]]
            if item.get("raw") and not item.get("error"):
                rows[doc["sha256"], doc["prompt_source"]] = (item, filename)
    return rows


def infer(path, model, initial, previous):
    row = {**initial, "source_page": 1, "source_fragment": "", "source_bbox": None,
           "title": "", "prediction": "", "raw": "", "inference_seconds": 0.0}
    with pdfplumber.open(path) as pdf:
        page = visible_page(pdf.pages[0])
        header = page.crop((page.width*.03, 0, page.width*.97, page.height*.47)).extract_text(x_tolerance=2) or ""
        if not header.strip():
            row.update(status="no_visible_header", reason="На первой странице в области заголовка нет текста.")
            return row
        client = model()
        tokens = client.tokenizer.encode(header, add_special_tokens=False)[:176]
        prompt = client.tokenizer.decode(tokens, clean_up_tokenization_spaces=False)
        row["prompt_sha256"] = signed_hash(client.prompt(prompt, TEMPLATE))
        begin = time.perf_counter()
        try:
            old = previous.get((initial["sha256"], prompt))
            if old:
                saved, filename = old
                row.update(raw=saved["raw"], prediction=saved["prediction"], original_inference_seconds=saved["seconds"],
                           generation_origin=filename)
            else:
                result = client.extract(prompt, TEMPLATE, max_new_tokens=80)
                values = result.data["publications"]
                row.update(raw=result.raw_output, prediction=values[0]["title"].strip() if len(values) == 1 else "",
                           generation_origin="current_run")
        except ExtractionError as error:
            row.update(status="invalid_model_response", raw=error.raw_output, reason=str(error))
            return row
        finally:
            row["inference_seconds"] = 0 if old else round(time.perf_counter()-begin, 3)
        candidate = flat(row["prediction"])
        if not candidate:
            row.update(status="empty_prediction", reason="Модель не вернула единственный заголовок.")
            return row
        # The same title must be independently identified as the COMPLETE heading.
        # A mere match of the model's first line or substring in the page does not qualify.
        normalized = candidate.casefold()
        metadata = pdf.metadata.get("Title")
        evidence = None
        if isinstance(metadata, str) and flat(metadata).casefold() == normalized:
            evidence = printed_metadata_title(page, metadata)
        if evidence:
            row.update(title=evidence["title"], source_fragment=evidence["source_fragment"],
                       source_bbox=evidence["source_bbox"], status="confirmed", method="complete_printed_metadata")
        else:
            printed = first_heading(page, page.extract_text(x_tolerance=2) or "")
            if printed and flat(printed["value"]).casefold() == normalized:
                row.update(title=printed["value"], source_fragment=printed["fragment"], source_bbox=printed["bbox"],
                           status="confirmed", method="independent_full_printed_heading")
            else:
                row.update(status="needs_review", reason="Полный печатный заголовок независимо не подтверждён.")
        return row


def main(corpus, adapter, output, threads):
    started = time.perf_counter()
    base = ROOT / "model_bundle/models/NuExtract-1.5-tiny"
    resources = [Path(__file__), ROOT / "model_bundle/article_analyzer/rag_documents.py",
                 ROOT / "model_bundle/article_analyzer/pdf_title_evidence.py",
                 ROOT / "model_bundle/article_analyzer/visible_pdf.py", ROOT / "model_bundle/article_analyzer/local_model.py",
                 ROOT / "model_bundle/requirements.lock.txt", base / "local-manifest.json",
                 adapter / "adapter_config.json", adapter / "adapter_model.safetensors"]
    hashes = {p.name: digest(p) for p in resources}
    base_revision = json.loads((base / "local-manifest.json").read_text(encoding="utf-8"))["revision"]
    previous = prior_evaluations(adapter, hashes, base_revision, threads)
    engine = signed_hash([hashes, TEMPLATE, threads, base_revision])
    sources = [path for split in ("dev", "test") for path in sorted((corpus/split).glob("*.pdf"))]
    if not sources or len({path.stem for path in sources}) != len(sources):
        raise ValueError("Expected distinct publication IDs in the dev and test PDF folders.")
    cache_dir = output.parent / "title-predictions"
    cache_dir.mkdir(exist_ok=True, parents=True)
    client = None
    def load():
        nonlocal client
        if client is None:
            client = LocalNuExtract(base, adapter_path=adapter, threads=threads)
            client.model.set_attn_implementation("sdpa")
        return client
    rows = {}
    counts = {"reused": 0, "new": 0}
    for path in sources:
        sha = digest(path)
        key = signed_hash([engine, path.name, sha])
        file = cache_dir / (path.stem + ".json")
        row = cached(file, key)
        if row is None:
            start = time.perf_counter()
            initial = {"source_file": path.name, "publication_id": path.stem, "sha256": sha,
                       "adapter_sha256": hashes["adapter_model.safetensors"], "base_revision": base_revision,
                       "inference_key": key}
            try:
                row = infer(path, load, initial, previous)
            except (ValueError, OSError, IndexError, NotImplementedError) as error:
                row = {**initial, "title": "", "source_page": 1, "source_fragment": "", "source_bbox": None,
                       "raw": "", "prediction": "", "status": "processing_failed",
                       "reason": f"{type(error).__name__}: {error}"}
            row["seconds"] = round(time.perf_counter()-start, 3)
            save_json(file, {**row, "record_sha256": signed_hash(row)})
            counts["new"] += 1
        else:
            counts["reused"] += 1
        rows[path.stem] = row
        save_json(output, rows)
        print(path.stem, row["status"], len(rows), "/", len(sources), flush=True)
    save_json(output.with_suffix(".run.json"), {"inputs": len(sources), "seconds": round(time.perf_counter()-started,3),
              **counts, "engine_sha256": engine, "hashes": hashes, "threads": threads})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--adapter", default=ROOT / "Результаты_RAG/training/epoch-2", type=Path)
    parser.add_argument("--out", default=ROOT / "Результаты_RAG/training/predicted-titles.json", type=Path)
    parser.add_argument("--threads", default=2, type=int)
    args = parser.parse_args()
    main(args.corpus.resolve(), args.adapter.resolve(), args.out.resolve(), args.threads)

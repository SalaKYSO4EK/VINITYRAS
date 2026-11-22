"""Run the isolated RAG publication-title pilot with only the project's local weights."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import random
import re
import sys
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "model_bundle"))

import pdfplumber
import torch
from peft import LoraConfig, TaskType, get_peft_model

from article_analyzer.local_model import ExtractionError, LocalNuExtract
from article_analyzer.pdf_title_evidence import printed_metadata_title, printed_outline_title
from article_analyzer.visible_pdf import visible_page
from tools.interpret_criteria import save_json

TEMPLATE = {"publications": [{"title": ""}]}
GROUPS = {"train": range(1, 15), "selection": range(15, 18), "holdout": range(18, 22)}


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def citation(value, header):
    if not value or not value.strip():
        return None
    match = re.search(r"\s+".join(re.escape(word) for word in value.split()), header, re.IGNORECASE)
    return match[0] if match else None


def first_page(path):
    with pdfplumber.open(path) as document:
        page = visible_page(document.pages[0])
        box = [page.width * .03, 0, page.width * .97, page.height * .47]
        header = page.crop(tuple(box)).extract_text(x_tolerance=2) or ""
        verified = printed_metadata_title(page, document.metadata.get("Title"))
        if not verified and "Outlines" in document.doc.catalog:
            verified = printed_outline_title(page, path)
    with path.open("rb") as file:
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    return {"source_file": path.name, "sha256": digest, "source_page": 1, "region": box,
            "visible_header": header, "metadata_title": verified}


def targets_for(corpus, ids):
    with (corpus / "dev_gold/publications.csv").open(encoding="utf-8-sig", newline="") as file:
        return {row["publication_id"]: row["title"] for row in csv.DictReader(file) if row["publication_id"] in ids}


def export_printed_predictions(corpus, raw_files, out):
    """Export only complete predictions matching prominent first-page heading glyphs, using no reference table."""
    titles = {}
    for filename in raw_files:
        for row in json.loads(filename.read_text(encoding="utf-8")):
            if not row["source_fragment"]:
                continue
            path = corpus / "dev" / row["source_file"]
            with pdfplumber.open(path) as pdf:
                verified = printed_metadata_title(visible_page(pdf.pages[0]), row["prediction"])
            if verified:
                with path.open("rb") as stream:
                    checksum = hashlib.file_digest(stream, "sha256").hexdigest()
                titles[row["publication_id"]] = {**verified, "source_file": path.name, "sha256": checksum,
                    "method": "lora_title_verified_against_complete_visible_heading"}
    save_json(out, titles)


def bind_predictions(out, epoch, revision, threads):
    raw_files = [f"epoch-{epoch}-selection.raw.json", "adapter-holdout.raw.json"]
    hashes = {name: hashlib.sha256((out/name).read_bytes()).hexdigest() for name in ["inputs.json", *raw_files]}
    weights = out / f"epoch-{epoch}" / "adapter_model.safetensors"
    save_json(out / "prediction-provenance.json", {"epoch": f"epoch-{epoch}", "base_revision": revision,
              "threads": threads, "adapter_sha256": hashlib.sha256(weights.read_bytes()).hexdigest(),
              "raw_files": raw_files, "files": hashes})


def evaluate(client, split, sources, truth, out):
    """Score only after saving predictions; the target is never passed to generation."""
    results = []
    for doc_id in split:
        source = sources[doc_id]
        row = {"publication_id": doc_id, "source_file": source["source_file"], "source_page": 1,
               "prediction": "", "raw": "", "error": "", "seconds": 0.0, "source_fragment": None}
        if not source["prompt_source"]:
            row["error"] = "no_visible_text_on_first_page"
        else:
            start = time.perf_counter()
            try:
                generated = client.extract(source["prompt_source"], TEMPLATE, max_new_tokens=80)
                values = generated.data["publications"]
                row["raw"] = generated.raw_output
                if len(values) == 1:
                    row["prediction"] = values[0]["title"].strip()
                else:
                    row["error"] = "expected_exactly_one_title"
            except ExtractionError as exc:
                row.update(error=str(exc), raw=exc.raw_output)
            row["seconds"] = round(time.perf_counter()-start, 3)
        row["source_fragment"] = citation(row["prediction"], source["visible_header"])
        results.append(row)
    save_json(out.with_suffix(".raw.json"), results)
    for row in results:
        row["expected"] = truth[row["publication_id"]]
        row["exact"] = normalized(row["prediction"]) == normalized(row["expected"])
    result = {"total": len(results), "readable": sum(bool(sources[r["publication_id"]]["prompt_source"]) for r in results),
              "exact": sum(r["exact"] for r in results),
              "cited": sum(bool(r["source_fragment"]) for r in results),
              "exact_and_cited": sum(r["exact"] and bool(r["source_fragment"]) for r in results),
              "generation_seconds": round(sum(r["seconds"] for r in results), 3), "records": results}
    save_json(out, result)
    print(out.name, {k: v for k, v in result.items() if k != "records"}, flush=True)
    return result


def main(corpus, out, epochs, threads):
    if out.exists() and any(out.iterdir()):
        raise ValueError("Select a new empty output folder to preserve previous checkpoints.")
    out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    random.seed(42)
    torch.manual_seed(42)
    groups = {name: [f"D{n:03}" for n in nums] for name, nums in GROUPS.items()}
    report = {"task": "RAG publication title from first-page visible text", "split": groups, "seed": 42,
              "criterion": "exact normalized title AND contiguous source quote; prefer base or earlier epoch on tie",
              "normalization": "NFKC, case-fold, collapse whitespace; punctuation stays significant",
              "source_support": "contiguous first-page region text with case and whitespace variation only",
              "max_source_tokens": 176, "max_new_tokens": 80, "fields": TEMPLATE, "threads": threads,
              "rank": 8, "alpha": 16, "learning_rate": 0.0002, "optimizer": "AdamW", "dropout": 0.05,
              "modules": ["q_proj", "v_proj"], "epochs": epochs, "train_steps": []}
    save_json(out / "run.json", report)
    sources = {i: first_page(corpus / "dev" / (i + ".pdf")) for group in groups.values() for i in group}
    # Independently verified PDF metadata can be reused without reading any gold.
    verified = {i: {**s["metadata_title"], "source_file": s["source_file"], "sha256": s["sha256"]}
                for i, s in sources.items() if s["metadata_title"]}
    save_json(out / "pdf-metadata-titles.json", verified)
    report["prepare_seconds"] = round(time.perf_counter()-started, 3)
    setup = time.perf_counter()
    client = LocalNuExtract(ROOT / "model_bundle/models/NuExtract-1.5-tiny", threads=threads)
    client.model.set_attn_implementation("sdpa")
    report["model_load_seconds"] = round(time.perf_counter()-setup, 3)
    report["base_revision"] = client.revision
    for source in sources.values():
        token_ids = client.tokenizer.encode(source["visible_header"], add_special_tokens=False)[:176]
        source["prompt_source"] = client.tokenizer.decode(token_ids, clean_up_tokenization_spaces=False)
    save_json(out / "inputs.json", sources)
    train = targets_for(corpus, groups["train"])
    validation = targets_for(corpus, groups["selection"])
    train_ids = [i for i in groups["train"] if citation(train[i], sources[i]["prompt_source"])]
    report["train_exclusions"] = {i: ("no_visible_text" if not sources[i]["prompt_source"] else
                                      "gold_title_not_located_in_prompt")
                                  for i in groups["train"] if i not in train_ids}
    report["train_ids_used"] = train_ids
    save_json(out / "run.json", report)
    if not train_ids:
        raise ValueError("No training pairs were located in the visible source.")
    base_val = evaluate(client, groups["selection"], sources, validation, out / "base-selection.json")
    model = get_peft_model(client.model, LoraConfig(r=8, lora_alpha=16, lora_dropout=0.05,
                          target_modules=["q_proj", "v_proj"], bias="none", task_type=TaskType.CAUSAL_LM))
    client.model = model
    report["trainable_parameters"] = sum(p.numel() for p in model.parameters() if p.requires_grad)
    optimizer = torch.optim.AdamW((p for p in model.parameters() if p.requires_grad), lr=2e-4)
    best_score = -1
    for epoch in range(1, epochs+1):
        model.train()
        model.config.use_cache = False
        random.shuffle(train_ids)
        for doc_id in train_ids:
            tick = time.perf_counter()
            prompt = client.tokenizer.encode(client.prompt(sources[doc_id]["prompt_source"], TEMPLATE), add_special_tokens=False)
            reply = json.dumps({"publications": [{"title": train[doc_id]}]}, ensure_ascii=False, separators=(",", ":"))
            label = client.tokenizer.encode(reply, add_special_tokens=False) + [client.tokenizer.eos_token_id]
            optimizer.zero_grad(set_to_none=True)
            loss = model(input_ids=torch.tensor([prompt+label]), labels=torch.tensor([[-100]*len(prompt)+label])).loss
            if not torch.isfinite(loss):
                raise ValueError("Non-finite training loss")
            loss.backward()
            optimizer.step()
            report["train_steps"].append({"epoch": epoch, "publication_id": doc_id, "loss": round(loss.item(), 6),
                                          "tokens": len(prompt)+len(label), "seconds": round(time.perf_counter()-tick, 3)})
            save_json(out / "run.json", report)
            print("train", report["train_steps"][-1], flush=True)
        model.config.use_cache = True
        model.eval()
        model.save_pretrained(out / f"epoch-{epoch}", safe_serialization=True)
        val = evaluate(client, groups["selection"], sources, validation, out / f"epoch-{epoch}-selection.json")
        if val["exact_and_cited"] > best_score:
            best_epoch, best_score = epoch, val["exact_and_cited"]
    selected = "base" if base_val["exact_and_cited"] >= best_score else f"epoch-{best_epoch}"
    report["locked_selection"] = {"model": selected, "best_adapter": f"epoch-{best_epoch}",
                                  "base_correct": base_val["exact_and_cited"], "best_adapter_correct": best_score}
    save_json(out / "run.json", report)
    # Open the held-out labels only after the selection (including best adapter) has been committed.
    holdout = targets_for(corpus, groups["holdout"])
    with model.disable_adapter():
        evaluate(client, groups["holdout"], sources, holdout, out / "base-holdout.json")
    model.load_adapter(out / f"epoch-{best_epoch}", adapter_name="selected_epoch", is_trainable=False)
    model.set_adapter("selected_epoch")
    evaluate(client, groups["holdout"], sources, holdout, out / "adapter-holdout.json")
    report["training_seconds"] = round(sum(row["seconds"] for row in report["train_steps"]), 3)
    export_printed_predictions(corpus, [out / f"epoch-{best_epoch}-selection.raw.json", out / "adapter-holdout.raw.json"],
                               out / "adapter-confirmed-titles.json")
    bind_predictions(out, best_epoch, client.revision, threads)
    report["total_seconds"] = round(time.perf_counter()-started, 3)
    save_json(out / "run.json", report)
    print("Done", report["locked_selection"], report["total_seconds"], flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=2)
    parser.add_argument("--epochs", type=int, default=2)
    args = parser.parse_args()
    if args.epochs < 1 or args.threads < 1:
        parser.error("epochs and threads must both be positive")
    main(args.corpus.resolve(), args.out.resolve(), args.epochs, args.threads)

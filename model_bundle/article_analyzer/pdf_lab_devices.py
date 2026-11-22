"""Enrich table-identified samples using the saved NuExtract per-device adapter."""
from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sys
import time
import unicodedata

from article_analyzer.job_progress import report_phase
from article_analyzer.jv_methods import article_page_texts
from article_analyzer.local_model import ExtractionError, LocalNuExtract
from article_analyzer.photovoltaic_tables import compact
from tools.interpret_criteria import save_json


ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "artifacts/criteria/iteration-2026-11-21/nuextract-baseline/per-device"
FIELDS = ("device_role", "device_type", "architecture", "substrate_type", "active_area",
          "area_unit", "area_definition", "encapsulation")
TEMPLATE = dict.fromkeys(FIELDS, "")
TYPE = {"cell": r"(?:solar|photovoltaic|perovskite)\s+cells?\b|\bPSCs\b",
        "minimodule": r"\bmini[- ]?modules?\b", "module": r"(?<!mini[- ])\bmodules?\b"}
PATTERNS = {
    "device_role": {"control": r"\bcontrol\b", "target": r"\btarget\b", "record": r"\brecord\b"},
    "device_type": TYPE,
    "architecture": {v: rf"\b{v.replace('-', r'\s*[-–−‑]\s*')}\b" for v in
                     ("n-i-p", "p-i-n", "substrate", "superstrate")},
    "substrate_type": {v: rf"\b{v}\b" for v in ("rigid", "flexible")},
    "area_definition": {"designated illumination": r"\bdesignated illumination area\b",
                        "aperture": r"\baperture(?:\s+area)?\b", "total": r"\btotal\s+area\b"},
    "encapsulation": {v: rf"\b{v}\b" for v in ("unencapsulated", "encapsulated")},
}
AREA = re.compile(r"(?<![.\d])(\d+(?:[.,]\d+)?)\s*(cm|mm)\s*\^?\s*[²2]\b", re.I)
AREA_CONTEXT = re.compile(r"\b(?:active|device|cell|aperture|designated illumination|illuminated|masked|total)\s+area\b", re.I)
PROPERTY_CONTEXT = re.compile(r"\b(?:active area|aperture|illuminated area|mask area|architecture|superstrate|"
                              r"[np]\s*[-–−]\s*i\s*[-–−]\s*[np]|encapsulat\w*|substrates?|"
                              r"solar cells?|solar modules?|cell fabrication|device fabrication)\b", re.I)
TREATMENT_COMPARISON = re.compile(
    r"(?<![\w-])(?P<target>[\w-]+)[-–]treated\s+devices?\b[^;]{0,175}?"
    r"\bthan\s+(?:the\s+)?(?P<control>[\w-]+)[-–]treated\s+control\s+devices?\b", re.I)


def sample_inventory(records):
    known, ambiguous = {}, {}
    for row in records:
        source = row["sources"]["sample"]
        key = (row["page"], row["table"], row["sample"])
        if row.get("ambiguity") or not row["sample"].strip() or not source.get("bbox"):
            issue = row.get("ambiguity") or {"reason": "Нет подтверждённого обозначения и его координат.",
                                             "alternatives": []}
            location = row["sources"].get("cell", row["sources"]["value"])
            ambiguous.setdefault((*key, issue["reason"]), {
                "status": "ambiguous_sample", "page": row["page"], "table": row["table"],
                "sample": row["sample"], "bbox": location.get("bbox"), **issue})
            continue
        known.setdefault(key, {"page": row["page"], "table": row["table"], "sample": row["sample"],
                               "source": source, "label_origin": row["label_origin"],
                               "caption": row["sources"]["caption"]["text"]})
    return {"known": list(known.values()), "issues": list(ambiguous.values())}


def evidence_chunks(pages, label):
    exact = re.compile(r"(?<!\w)" + re.escape(re.sub(r"\s+", " ", label.strip())) + r"(?!\w)", re.I)
    scored = []
    for page, text in pages:
        sentences = re.split(r"(?<=[.!?])\s+|\n\s*\n", text)
        for sentence in sentences:
            clean = re.sub(r"\s+", " ", sentence.strip())
            if not clean:
                continue
            label_match = exact.search(clean)
            if len(label.strip()) < 3 and label_match:
                label_match = re.search(r"\b(?:sample|device|cell)\s+" + re.escape(label) + r"(?!\w)", clean, re.I)
            prop = PROPERTY_CONTEXT.search(clean)
            weight = 5 * bool(label_match) + 3 * bool(AREA_CONTEXT.search(clean)) + 2 * bool(prop)
            if weight:
                focus = (label_match or prop or AREA_CONTEXT.search(clean)).start()
                start = max(0, focus - 120)
                scored.append((weight, {"page": page, "quote": clean[start:start + 420],
                                        "sample_mentioned": bool(label_match)}))
    scored.sort(key=lambda item: -item[0])
    return [span for _, span in scored[:8]]


def source_matches(prediction, wanted, chunks):
    fields, unconfirmed = {}, {}
    for field in wanted:
        value = prediction[field].strip()
        if not value or field in {"active_area", "area_unit"}:
            continue
        patterns = PATTERNS[field]
        pattern = patterns.get(value)
        options = {key: [span for span in chunks if re.search(regex, span["quote"], re.I)
                         and (field != "device_role" or span["sample_mentioned"])
                         and (field != "architecture" or key != "substrate"
                              or re.search(r"substrate[- ](?:type|configuration|structure|geometry)", span["quote"], re.I))]
                   for key, regex in patterns.items()}
        matched = options.get(value, [])
        sample_matches = {key for key, spans in options.items() if any(span["sample_mentioned"] for span in spans)}
        applicable = sample_matches or {key for key, spans in options.items() if spans}
        if pattern and matched and applicable == {value}:
            span = next((s for s in matched if s["sample_mentioned"]), matched[0])
            fields[field] = {"value": value, "source": span}
        else:
            unconfirmed[field] = {"value": value, "reason": "no_unique_source"}
    if {"active_area", "area_unit"} & set(wanted):
        match_active_area(prediction, wanted, chunks, fields, unconfirmed)
    return fields, unconfirmed


def match_active_area(prediction, wanted, chunks, fields, unconfirmed):
    raw = prediction["active_area"].strip()
    unit = unicodedata.normalize("NFKC", prediction["area_unit"]).lower().replace("^", "").replace(" ", "")
    if not raw and not unit:
        return
    try:
        quantity = Decimal(raw.replace(",", "."))
    except InvalidOperation:
        quantity = None
    if quantity is not None and (not quantity.is_finite() or quantity <= 0):
        quantity = None
    candidates = [(Decimal(match[1].replace(",", ".")), match[2].lower() + "2", span)
                  for span in chunks if AREA_CONTEXT.search(span["quote"]) for match in AREA.finditer(span["quote"])]
    applicable = [item for item in candidates if item[2]["sample_mentioned"]] or candidates
    if unit == "cm2" and applicable and {(n, u) for n, u, _ in applicable} == {(quantity, unit)}:
        for field, value in (("active_area", str(quantity)), ("area_unit", unit)):
            if field in wanted:
                fields[field] = {"value": value, "source": applicable[0][2]}
    else:
        for field in {"active_area", "area_unit"} & set(wanted):
            if prediction[field].strip():
                unconfirmed[field] = {"value": prediction[field], "reason": "no_unique_area_with_unit"}


def comparison_roles(pages, samples):
    """Use an explicit treatment-versus-control comparison only for its table cohort."""
    cohorts = defaultdict(set)
    for item in samples:
        cohorts[item["page"], item["table"]].add(compact(item["sample"]))
    paired = defaultdict(list)
    for page, text in pages:
        for statement in TREATMENT_COMPARISON.finditer(text):
            roles = {compact(statement[role]): role for role in ("target", "control")}
            if len(roles) != 2:
                continue
            source = {"page": page, "quote": statement[0], "start": statement.start(), "end": statement.end()}
            for key, cohort in cohorts.items():
                if roles.keys() <= cohort:
                    paired[key].append((roles, source))
    result = {}
    for item in samples:
        key = (item["page"], item["table"], item["sample"])
        if key[:2] not in paired:
            continue
        label = compact(item["sample"])
        available = {roles[label]: source for roles, source in reversed(paired[key[:2]]) if label in roles}
        if len(available) == 1:
            role, source = next(iter(available.items()))
            result[key] = {"value": role, "source": source, "basis": "explicit_treatment_comparison"}
        else:
            result[key] = {"alternatives": available}
    return result


def infer_sample(client, item, pages, wanted, role_context=None):
    chunks = evidence_chunks(pages, item["sample"])
    caption = {"page": item["page"], "quote": item["caption"], "sample_mentioned": False}
    if caption["quote"]:
        chunks.insert(0, caption)
    prompt = "Device under analysis\n" + item["sample"] + "\n\nPDF evidence\n" + "\n\n".join(
        f"[PDF page {span['page']}]\n{span['quote']}" for span in chunks)
    start = time.perf_counter()
    try:
        answer = client.extract(prompt, TEMPLATE, max_new_tokens=200)
    except ExtractionError as exc:
        result = {"status": "truncated_response" if exc.truncated else "invalid_response",
                  "reason": str(exc), "raw": exc.raw_output, "input": prompt,
                  "seconds": round(time.perf_counter() - start, 3)}
        if role_context and role_context.get("value"):
            result["fields"] = {"device_role": role_context}
        return result
    requested = [name for name in wanted if name != "device_role"] if role_context is not None else wanted
    fields, unconfirmed = source_matches(answer.data, requested, chunks)
    if role_context is not None:
        predicted = answer.data["device_role"].strip()
        confirmed = role_context.get("value")
        if confirmed:
            fields["device_role"] = role_context
        if predicted and predicted != confirmed:
            unconfirmed["device_role"] = {"value": predicted, "reason": "not_supported_by_treatment_comparison",
                                          "comparison": role_context}
    return {"status": "source_supported" if fields else "no_supported_properties", "input": prompt,
            "raw": answer.raw_output, "prediction": answer.data, "fields": fields, "unconfirmed": unconfirmed,
            "seconds": round(time.perf_counter() - start, 3)}


def main(args):
    request_path, output_path, threads = args
    requests = json.loads(Path(request_path).read_text(encoding="utf-8"))
    base_path = ROOT / "models/NuExtract-1.5-tiny"
    model = None
    titles = requests.get("titles")
    if titles:
        from article_analyzer.pdf_lab_titles import extract_titles
        report_phase("extract_titles")
        with Path(titles["log"]).open("w", encoding="utf-8") as log, redirect_stdout(log), redirect_stderr(log):
            start = time.perf_counter()
            model = LocalNuExtract(base_path, threads=int(threads))
            extract_titles(model, titles["output"], titles["paths"], round(time.perf_counter() - start, 3),
                           requests.get("pages_dir"))
        Path(titles["finished"]).touch()
    report_phase("extract_devices")
    manifest = json.loads((ADAPTER.parent / "manifest.json").read_text(encoding="utf-8"))
    hashes = manifest["adapters"]["per-device"]["sha256"]
    for name, expected in hashes.items():
        with (ADAPTER / name).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                raise ValueError(f"Adapter checksum mismatch: {name}")
    start = time.perf_counter()
    if model is None:
        model = LocalNuExtract(base_path, threads=int(threads))
    model.attach_adapter(ADAPTER)
    model.model = model.model.merge_and_unload(safe_merge=True)
    model.model.set_attn_implementation("sdpa")
    report = {"load_seconds": round(time.perf_counter() - start, 3), "base_revision": model.revision,
              "adapter_hashes": hashes, "publications": {}}
    output = Path(output_path)
    save_json(output, report)
    for index, article in enumerate(requests["documents"], 1):
        path = Path(article["path"])
        report_phase("extract_devices", pdf_index=index, pdf_total=len(requests["documents"]), file=path.name)
        pages_file = Path(requests["pages_dir"], path.name + ".json") if requests.get("pages_dir") else None
        if pages_file and pages_file.exists():
            pages = list(enumerate(json.loads(pages_file.read_text(encoding="utf-8")), 1))
        else:
            pages = list(article_page_texts(path).items())
        entries = []
        report["publications"][article["publication_id"]] = entries
        roles = comparison_roles(pages, article["samples"]) if "device_role" in requests["fields"] else {}
        for item in article["samples"]:
            role = roles.get((item["page"], item["table"], item["sample"]))
            result = infer_sample(model, item, pages, requests["fields"], role)
            entries.append(dict(item, **result))
            save_json(output, report)
        print(article["publication_id"], dict(Counter(entry["status"] for entry in entries)), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])

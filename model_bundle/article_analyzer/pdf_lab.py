"""A local job from compiled criteria to evidence, review and submission tables."""
from __future__ import annotations

from collections import Counter, defaultdict
import csv
import hashlib
from html import escape
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from zipfile import ZIP_DEFLATED, ZipFile

from pypdf.errors import PdfReadError

from article_analyzer.criteria import LocalCriteriaInterpreter
from article_analyzer.jv_methods import article_page_texts, apply_illumination_methods, illumination_methods, is_performance_group
from article_analyzer.pdf_lab_devices import FIELDS as DEVICE_FIELDS, sample_inventory
from article_analyzer.photovoltaic_tables import CONFIG, compact, detect_pdf
from article_analyzer.pv_checks import AGGREGATION, audit, normalize, table_context
from article_analyzer.pv_table_prose import apply_table_prose, table_prose
from tools.check_submission import main as check_submission
from tools.interpret_criteria import read_source, save_json
from tools.interpret_pdf_tables import requested_metrics


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "submission_template"
STATUS = {"failed": "Есть замечание", "passed": "Проверено", "unavailable": "Проверка невозможна",
          "needs_clarification": "Требует уточнения", "not_checked": "Нет применимой проверки"}
JV = {"PCE", "Voc", "Jsc", "FF"}


def compile_criteria(path, out, threads=4, pages=None):
    source = read_source(path, pages)
    interpreter = LocalCriteriaInterpreter(ROOT / "models/criteria/qwen2.5-1.5b-instruct-q4_k_m.gguf",
                                           ROOT / "models/criteria/llama-b8157/llama-completion.exe", threads)
    begin = time.perf_counter()
    try:
        plan = interpreter.interpret(source, 1800)
    finally:
        save_json(out / "qwen-raw.json", {"responses": interpreter.raw_responses})
    bundle = project_criteria(plan, source)
    bundle["seconds"] = round(time.perf_counter() - begin, 3)
    save_json(out / "criteria-plan.json", bundle)
    return bundle


def project_criteria(plan, source):
    groups = plan["extraction"]["groups"]
    names = {field["name"] for group in groups for field in group["fields"]}
    local_names = {compact(name).rsplit(".", 1)[-1] for name in names}
    metrics = requested_metrics(plan, CONFIG)
    enum_name = CONFIG["metric_enum_field"]
    citations, unsupported = [], []
    if not metrics and enum_name in local_names:
        aliases = {compact(alias): metric for metric, options in CONFIG["criterion_fields"].items()
                   for alias in options}
        pattern = re.compile(r"\b" + re.escape(enum_name) + r"\s*\(([^()]{1,400})\)", re.I)
        for page, text in source.pages.items():
            for match in pattern.finditer(text):
                options = re.split(r"\s*[/,|]\s*", match[1])
                listed = []
                for option in options:
                    item = re.match(r"^[\w]+(?:[ -][\w]+){0,4}", option.strip())
                    if not item:
                        continue
                    value = item[0].split(" и др.")[0].removesuffix(" и др")
                    metric = aliases.get(compact(value))
                    if metric:
                        listed.append(metric)
                    else:
                        unsupported.append(value)
                if listed:
                    citations.append({"page": page, "quote": match[0], "metrics": listed})
                    metrics.extend(listed)
        metrics = list(dict.fromkeys(metrics))
    title = bool(local_names & {"title", "article_title", "publication_title"})
    device_groups = {"devices", "device", "samples", "sample", "device_properties"}
    device_names = {compact(field["name"]).rsplit(".", 1)[-1] for group in groups for field in group["fields"]
                    if compact(group.get("name", "")) in device_groups
                    or compact(field["name"]).split(".")[0] in device_groups}
    device_fields = [name for name in DEVICE_FIELDS if name in device_names]
    recognized = {compact(alias) for metric in metrics for alias in CONFIG["criterion_fields"][metric]}
    recognized |= {"title", "article_title", "publication_title", "publication_id",
                   "result_id", "measurement_id", "device_id", "device_label", "raw_value", "raw_unit",
                   "normalized_value", "normalized_unit", "metric_value", "metric_unit",
                   "source_page", "source_table", "source_fragment", "extraction_status",
                   "scan_direction", "aggregation", "light_intensity", "spread_value", "n_devices", enum_name}
    recognized.update(device_fields)
    return {"plan": plan, "metrics": metrics, "title": title, "device_fields": device_fields,
            "metric_enum_sources": citations,
            "unsupported_metrics": sorted(set(unsupported)),
            "other_fields": sorted(name for name in names if compact(name).rsplit(".", 1)[-1] not in recognized)}


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def title_from_model(path, client):
    from pypdf import PdfReader

    text = PdfReader(path).pages[0].extract_text() or ""
    if not text.strip():
        return {"title": "", "status": "page_unreadable"}
    candidate = client.extract(text[:6500], {"publications": [{"title": ""}]}, max_new_tokens=112)
    values = candidate.data["publications"]
    title = values[0]["title"].strip() if len(values) == 1 else ""
    trimmed = re.sub(r"\s+", " ", title)
    located = bool(trimmed and trimmed in re.sub(r"\s+", " ", text))
    return {"title": title if located else "", "status": "extracted" if located else "unconfirmed",
            "source_page": 1 if located else None, "source_fragment": title if located else "",
            "raw": candidate.raw_output, "seconds": candidate.seconds}


def cohort_key(row):
    context = row["context"]
    aggregation = context.get("aggregation", {})
    if aggregation.get("scope") not in {"row", "column"} or aggregation["value"] not in ("champion", "mean"):
        return None
    if row.get("ambiguity") or context["ambiguities"] or row["metric"] != "PCE":
        return None
    label = row["sample"]
    if not row["sources"].get("aggregation"):
        label = re.sub(AGGREGATION[aggregation["value"]], "", label, flags=re.I).strip(" ()[],:-")
    if not label:
        return None
    return (row["page"], row["table"], compact(label), row["measurement_type"], row["condition"],
            *(context.get(name, {}).get("value", "") for name in ("pin", "spectrum", "insolation")))


def table_records(path, metrics, *, include_devices=False):
    start = time.perf_counter()
    source = detect_pdf(path)
    prepared_at = time.perf_counter()
    grouped = defaultdict(list)
    table_metrics = defaultdict(set)
    for cell in source["records"]:
        table_metrics[cell["page"], cell["table"]].add(cell["metric"])
    for cell in source["records"]:
        table_key = cell["page"], cell["table"]
        key = (*table_key, cell["sample"], cell["condition"], cell["occurrence"])
        context = table_context(cell)
        measurement_type = ""
        if cell["metric"] == "Jsc_EQE":
            measurement_type = "EQE"
        elif cell["metric"] in JV and len(table_metrics[table_key] & JV) >= 2:
            measurement_type = "J-V"
        if cell["metric"] == "Jsc_EQE" and cell["condition"]:
            context["layout_direction_raw"] = cell["condition"]
            context["scan_direction"] = ""
            context["scan_direction_raw"] = ""
            context["scan_direction_source"] = None
            context["ambiguities"]["jv_pair"] = {
                "reason": "Ячейка EQE напечатана в строке ВАХ, но относится к иному измерению.",
                "alternatives": sorted({c["condition"] for c in source["records"]
                                        if c["metric"] == "Jsc" and c["sample"] == cell["sample"]
                                        and (c["page"], c["table"]) == table_key and c["condition"]}),
            }
        grouped[key].append(dict(cell, context=context, normalized=normalize(cell),
                                 publication_id=path.stem, measurement_type=measurement_type))

    jv_groups = [group for group in grouped.values() if any(row["measurement_type"] == "J-V" for row in group)]
    method_warning = ""
    if jv_groups:
        try:
            pages = article_page_texts(path)
            methods = illumination_methods(path, pages=pages)
            prose = table_prose(pages)
        except (PdfReadError, OSError, ValueError, NotImplementedError) as exc:
            methods, prose = [], {}
            method_warning = f"Не удалось прочитать текст методики: {exc}"
        linked_tables = {compact(candidate["table"]) for candidate in methods if candidate.get("table")}
        for group in jv_groups:
            can_link = is_performance_group(group) or compact(group[0]["table"]) in linked_tables
            needs_light = can_link and any("pin" not in row["context"] or "spectrum" not in row["context"] for row in group)
            for row in group:
                if row["measurement_type"] == "J-V":
                    apply_table_prose(row, prose)
                    if needs_light:
                        apply_illumination_methods(row["context"], methods, basis={
                            "measurement_type": row["measurement_type"],
                            "table": row["table"],
                            "table_source": dict(row["sources"]["caption"], page=row["page"]),
                            "direction_source": row["sources"].get("direction"),
                        })
    methods_at = time.perf_counter()

    audit_groups = []
    for group in grouped.values():
        by_aggregation = defaultdict(list)
        for row in group:
            aggregation = row["context"].get("aggregation", {}).get("value", "")
            by_aggregation[aggregation].append(row)
        audit_groups.extend(by_aggregation.values())

    index, means = Counter(), defaultdict(list)
    for group in audit_groups:
        for row in group:
            kind = "R" if row["metric"] in metrics else "A"
            index[kind] += 1
            row["result_id"] = f"{kind}{index[kind]:04}"
            cohort = cohort_key(row)
            if cohort and row["context"]["aggregation"]["value"] == "mean":
                means[cohort].append(row)

    records, checks = [], []
    for group in audit_groups:
        visible = [r for r in group if r["metric"] in metrics]
        if not visible:
            continue
        records.extend(visible)
        bandgaps = [r for r in group if r["metric"].startswith("Eg_") and r["normalized"]["value"]
                    and not r.get("ambiguity")]
        integrated = [r for r in group if r["metric"] == "Jsc_EQE" and not r["context"]["ambiguities"]
                      and not r.get("ambiguity") and r["label_origin"] == "printed_column"]
        pce = next((r for r in group if r["metric"] == "PCE"), None)
        cohorts = (means.get(cohort_key(pce), []) if pce and
                   pce["context"].get("aggregation", {}).get("value") == "champion" else [])
        comparison = audit(group, integrated, bandgaps, cohorts)
        for check in comparison:
            check.update(publication_id=path.stem, sample=group[0]["sample"], page=group[0]["page"],
                         table=group[0]["table"], scan=group[0]["condition"])
            check["visible_ids"] = [r["result_id"] for r in visible
                                    if any(r["result_id"] == a["result_id"] for a in check["inputs"])]
            if check["visible_ids"]:
                checks.append(check)
        for row in visible:
            row["extraction_status"] = "present_not_extractable" if row.get("ambiguity") else "extracted"
            states = {c["status"] for c in comparison if row["result_id"] in c["visible_ids"]}
            if row.get("ambiguity") or row["context"]["ambiguities"] or row["normalized"]["status"] in {
                "needs_clarification", "unit_unresolved"
            }:
                states = {"needs_clarification"} | (states & {"failed"})
            row["check_status"] = next((s for s in ("failed", "passed", "needs_clarification", "unavailable")
                                        if s in states), "not_checked")
            row["check_status_label"] = STATUS[row["check_status"]]
    elapsed = time.perf_counter()
    counts = Counter(r["metric"] for r in records if not r.get("ambiguity"))
    unresolved = Counter(r["metric"] for r in records if r.get("ambiguity"))
    coverage = {}
    for metric in metrics:
        status = "not_found_in_recognized_tables"
        if counts[metric]:
            status = "extracted"
        elif unresolved[metric]:
            status = "present_not_extractable"
        coverage[metric] = {"records": counts[metric], "unresolved": unresolved[metric], "status": status}
    output = {"publication_id": path.stem, "file": path.name, "sha256": file_digest(path),
            "records": records, "checks": checks, "candidate_regions": source["candidate_regions"],
            "review_regions": source["review_regions"],
            "method_warning": method_warning,
            "coverage": coverage,
            "seconds": {"layout_and_binding": round(prepared_at - start, 3),
                        "methods_and_context": round(methods_at - prepared_at, 3),
                        "normalization_and_checks": round(elapsed - methods_at, 3)}}
    if include_devices:
        inventory = sample_inventory(row for row in source["records"] if row["metric"] in JV)
        output["device_inventory"] = inventory
    return output


def project_tables(documents):
    tables = {p.stem: [] for p in sorted(TEMPLATE.glob("*.csv"))}
    for doc in documents:
        pid = doc["publication_id"]
        base = {"publication_id": pid}
        tables["publications"].append(dict(base, title=doc.get("title", {}).get("title", "")))
        devices, measurements = {}, {}
        attributes = {(sample["page"], sample["table"], sample["sample"]): sample["fields"]
                      for sample in doc.get("device_attributes", []) if sample.get("fields")}
        device_rows = {}
        for row in doc["records"]:
            context = row["context"]
            normalized = row["normalized"]
            table = row["table"]
            known = not row.get("ambiguity")
            dkey = (row["page"], table, row["sample"])
            if known and dkey not in devices:
                device_id = f"D{len(devices) + 1:03}"
                devices[dkey] = device_id
                device_rows[dkey] = dict(base, device_id=device_id, device_label=row["sample"],
                                         note=f"Обозначение в {table}, стр. PDF {row['page']}.")
                fabrication = context.get("fabrication")
                if fabrication:
                    temperature = fabrication["annealing_temperature"]
                    device_rows[dkey]["note"] += (
                        f" Изготовление: {fabrication['agent']}, {fabrication['concentration']} {fabrication['unit']}, "
                        f"отжиг {temperature['value']} {temperature['unit']} (стр. PDF {fabrication['source']['page']}).")
            device_id = devices.get(dkey, "") if known else ""
            pin = context.get("pin", {})
            conditions = [context["scan_direction_raw"], context.get("insolation", {}).get("value", "")]
            linkage = context.get("illumination_link")
            if linkage and linkage["status"] == "needs_review":
                conditions.append("Световой режим требует сверки")
            elif linkage and pin.get("source"):
                label = linkage.get("figure", {}).get("label", "методика")
                conditions.append(f"Освещение: {label}, стр. {pin['source']['page']}")
            agg = context.get("aggregation", {}).get("value", "")
            mkey = (dkey, row["occurrence"], tuple(conditions), row["measurement_type"], agg)
            if known and mkey not in measurements:
                mid = f"M{len(measurements) + 1:03}"
                measurements[mkey] = mid
                tables["measurements"].append(dict(
                    base, measurement_id=mid, device_id=device_id, measurement_type=row["measurement_type"],
                    spectrum=context.get("spectrum", {}).get("value", ""), light_intensity=pin.get("value", ""),
                    intensity_unit=pin.get("unit", ""), scan_direction=context["scan_direction"],
                    note=" | ".join(c for c in conditions if c)))
            mid = measurements.get(mkey, "") if known else ""
            row.update(device_id=device_id, measurement_id=mid)
            notes = [normalized["origin"], *context["ambiguities"]]
            statistics = context.get("aggregation", {})
            if agg == "mean" and statistics.get("scope") == "series" and statistics.get("n_devices"):
                notes.append(f"Среднее по {statistics['n_devices']} приборам, стр. {statistics['source']['page']}")
            if not known:
                notes.append(row["ambiguity"]["reason"])
            tables["results"].append(dict(
                base, result_id=row["result_id"], device_id=device_id, measurement_id=mid,
                metric_name=row["metric"], raw_value=row["value_raw"], raw_unit=row["unit_raw"],
                metric_value=normalized["value"] if known else "", metric_unit=normalized["unit"] if known else "",
                normalized_value=normalized["value"] if known else "", normalized_unit=normalized["unit"] if known else "",
                aggregation=agg if known else "", spread_value=normalized.get("spread_value", "") if known else "",
                n_devices=context.get("aggregation", {}).get("n_devices", "") if known else "",
                source_page=row["page"], source_table=table,
                source_fragment=row["sources"]["value"]["text"], extraction_status=row["extraction_status"],
                note=" | ".join(n for n in notes if n)))
        for sample in doc.get("device_inventory", {}).get("known", []):
            key = (sample["page"], sample["table"], sample["sample"])
            if key not in device_rows:
                device_rows[key] = dict(base, device_id=f"D{len(device_rows) + 1:03}", device_label=sample["sample"],
                                        note=f"Обозначение в {sample['table']}, стр. PDF {sample['page']}.")
        for key, device in device_rows.items():
            fields = attributes.get(key, {})
            if fields:
                device.update({field: evidence["value"] for field, evidence in fields.items()})
                device["note"] += " Свойства и источники в records.json."
                role = fields.get("device_role", {})
                if role.get("basis") == "explicit_treatment_comparison":
                    device["note"] += f" Роль в сравнении: стр. PDF {role['source']['page']}."
            tables["devices"].append(device)
        for check in doc["checks"]:
            if check["status"] != "failed":
                continue
            description = (f"{check['sample']}: в статье {check.get('reported', '')} {check.get('unit', '')}; "
                           f"расчёт или сравнение {check.get('calculated', '')} {check.get('unit', '')}. "
                           f"{check.get('formula', '')}")
            visible_first = next(i for i in check["inputs"] if i["result_id"] in check["visible_ids"])
            n = sum(r["publication_id"] == pid for r in tables["issues"])
            tables["issues"].append(dict(base, issue_id=f"I{n + 1:03}", record_ids=";".join(check["visible_ids"]),
                                         check_name=check["name"], description=description,
                                         source_page=check["page"], source_fragment=visible_first["value_raw"]))
    return tables


def write_submission(tables, out):
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in tables.items():
        with (TEMPLATE / f"{name}.csv").open(encoding="utf-8-sig", newline="") as source:
            header = next(csv.reader(source))
        with (out / f"{name}.csv").open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=header)
            writer.writeheader()
            writer.writerows(rows)


def write_review(path, rows):
    columns = ("Статья", "Образец", "Развёртка", "Показатель", "Как в PDF", "После нормализации",
               "Проверка", "Источник")
    content = ["<!doctype html><html lang='ru'><meta charset='utf-8'><title>Научные данные</title>",
               "<style>body{font:16px system-ui;margin:32px;color:#192b2b}table{border-collapse:collapse;width:100%}"
               "th,td{text-align:left;padding:10px;border-bottom:1px solid #ccd8d3}tr:nth-child(even){background:#eef5f1}"
               "th{background:#193731;color:white}a{color:#00654a}</style><h1>Научные данные</h1><table><thead><tr>"]
    content.extend(f"<th scope='col'>{text}</th>" for text in columns)
    content.append("</tr></thead><tbody>")
    for r in rows:
        raw = r["sources"].get("cell", r["sources"]["value"])["text"]
        spread = r["normalized"].get("spread_value")
        normalized = " ".join(filter(None, (r["normalized"]["value"], f"± {spread}" if spread else "",
                                              r["normalized"]["unit"])))
        values = (r["publication_id"], r["sample"], r["condition"], r["metric"],
                  f"{raw} {r['unit_raw']}", normalized,
                  r["check_status_label"])
        content.append("<tr>" + "".join(f"<td>{escape(str(value))}</td>" for value in values))
        source = " · ".join((r["publication_id"] + ".pdf", f"стр. {r['page']}", r["table"],
                             r["sources"]["header"]["text"]))
        content.append(f"<td>{escape(source)}</td></tr>")
    content.append("</tbody></table></html>")
    path.write_text("".join(content), encoding="utf-8")


def launch_model(module, log_path, args, timeout, errors):
    command = [sys.executable, "-B", "-X", "utf8", "-m", module, *map(str, args)]
    with log_path.open("w", encoding="utf-8") as log:
        try:
            child = subprocess.run(command, cwd=ROOT, stdout=log, stderr=log, check=False,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            errors.append({"phase": module.rsplit(".", 1)[-1], "file": "", "error": str(exc)})
            return
    if child.returncode:
        errors.append({"phase": module.rsplit(".", 1)[-1], "file": "", "error":
                       f"NuExtract завершился с кодом {child.returncode}. См. {log_path.name}."})


def run_device_stage(pdfs, documents, fields, out, threads, errors):
    from datetime import datetime

    request = out / "device-requests.json"
    response = out / "device-responses.json"
    requests = []
    by_name = {path.name: path.resolve() for path in pdfs}
    with (out / "device-journal.jsonl").open("w", encoding="utf-8") as log:
        def event(pid, entry):
            message = {"time": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                       "publication_id": pid, **entry}
            log.write(json.dumps(message, ensure_ascii=False) + "\n")
            log.flush()

        for doc in documents:
            pid = doc["publication_id"]
            inventory = doc.get("device_inventory", {})
            for issue in inventory.get("issues", []):
                event(pid, issue)
            samples = inventory.get("known", [])
            if samples:
                requests.append({"path": str(by_name[doc["file"]]), "publication_id": pid, "samples": samples})
                event(pid, {"status": "samples_identified", "count": len(samples)})
            else:
                reason = ("Нет однозначно привязанных обозначений." if inventory.get("issues")
                          else "Разбор полных PDF не выделил образцы в распознанных таблицах ВАХ."
                          if "device_inventory" in doc else "Разбор PDF не завершился.")
                event(pid, {"status": "empty_sample_list", "reason": reason})
        if not requests:
            return {}

        response.unlink(missing_ok=True)
        save_json(request, {"fields": fields, "documents": requests})
        total_samples = sum(len(entry["samples"]) for entry in requests)
        launch_model("article_analyzer.pdf_lab_devices", out / "nuextract-devices.log",
                     (request.resolve(), response.resolve(), threads), 180 + 40 * total_samples, errors)
        report = json.loads(response.read_text(encoding="utf-8")) if response.is_file() else {}
        for doc in documents:
            pid = doc["publication_id"]
            attrs = report.get("publications", {}).get(pid, [])
            doc["device_attributes"] = attrs
            completed = {(a["page"], a["table"], a["sample"]) for a in attrs}
            for sample in doc.get("device_inventory", {}).get("known", []):
                if (sample["page"], sample["table"], sample["sample"]) not in completed:
                    event(pid, dict(sample, status="worker_interrupted", reason="Ответ для образца не сохранён."))
            for item in attrs:
                event(pid, {key: item[key] for key in
                           ("page", "table", "sample", "status", "reason", "seconds", "unconfirmed") if key in item})
        return report


def run_documents(paths, compiled, out, threads=4, progress=None):
    from contextlib import redirect_stdout
    from io import StringIO

    start = time.perf_counter()
    out.mkdir(exist_ok=True, parents=True)
    pdfs = sorted(paths)
    if not pdfs:
        raise ValueError("В задании нет PDF.")
    documents, errors = [], []
    model_load_seconds = 0
    device_fields = compiled.get("device_fields", [])
    for index, path in enumerate(pdfs, 1):
        if progress:
            progress(f"PDF {index}/{len(pdfs)} · {path.name}")
        try:
            if compiled["metrics"] or device_fields:
                doc = table_records(path, compiled["metrics"], include_devices=bool(device_fields))
            else:
                doc = {"publication_id": path.stem, "file": path.name, "records": [], "checks": [],
                       "coverage": {}, "sha256": file_digest(path)}
        except Exception as exc:
            errors.append({"file": path.name, "phase": "layout", "error": f"{type(exc).__name__}: {exc}"})
            doc = {"publication_id": path.stem, "file": path.name, "records": [], "checks": [],
                   "coverage": {m: {"records": 0, "status": "processing_failed"} for m in compiled["metrics"]}}
        documents.append(doc)
        save_json(out / "progress.json", {"documents": documents, "errors": errors})
    if compiled["title"]:
        if progress:
            progress("NuExtract · проверка названий по первым страницам.")
        response = out / "title-responses.json"
        response.unlink(missing_ok=True)
        launch_model("article_analyzer.pdf_lab_titles", out / "nuextract.log",
                     (response.resolve(), threads, *(path.resolve() for path in pdfs)), 90 + 60 * len(pdfs), errors)
        if response.is_file():
            titles = json.loads(response.read_text(encoding="utf-8"))
            model_load_seconds = titles["load_seconds"]
            errors.extend(titles["errors"])
            for doc in documents:
                doc["title"] = titles["responses"].get(doc["file"], {"status": "processing_failed"})
    devices = {}
    if device_fields:
        if progress:
            progress("NuExtract LoRA · свойства образцов с найденными обозначениями.")
        devices = run_device_stage(pdfs, documents, device_fields, out, threads, errors)
    tables = project_tables(documents)
    write_submission(tables, out / "csv")
    rows = [row for doc in documents for row in doc["records"]]
    checks = [check for doc in documents for check in doc["checks"]]
    write_review(out / "review.html", rows)
    validation = StringIO()
    with redirect_stdout(validation):
        code = check_submission(out / "csv", pdfs[0].parent if len({p.parent for p in pdfs}) == 1 else None)
    (out / "check-submission.txt").write_text(validation.getvalue(), encoding="utf-8")
    result = {"requested_metrics": compiled["metrics"], "requested_device_fields": device_fields,
              "other_fields": compiled["other_fields"],
              "documents": documents, "records": rows, "checks": checks, "errors": errors,
              "submission": {"exit_code": code, "rows": {name: len(value) for name, value in tables.items()},
                             "source_mode": "separate bboxes in records.json; PDF fragment validator uses the numeric cell"},
              "seconds": {"run": round(time.perf_counter() - start, 3), "nuextract_load": model_load_seconds,
                          "nuextract_inference": round(sum(d.get("title", {}).get("seconds", 0) for d in documents), 3),
                          "device_model_load": devices.get("load_seconds", 0),
                          "device_model_inference": round(sum(a["seconds"] for d in documents
                                                              for a in d.get("device_attributes", [])), 3)},
              "limitations": ["Материалы и слои приборов не извлекаются автоматически этим маршрутом.",
                              "Свойства приборов извлекаются для обозначений, установленных по таблицам ВАХ.",
                              "Статус not_found_in_recognized_tables не устанавливает отсутствие сведений в PDF.",
                              "Обрабатываются текстовые таблицы PDF с распознаваемой разметкой."]}
    save_json(out / "records.json", result)
    with ZipFile(out / "submission.zip", "w", compression=ZIP_DEFLATED) as archive:
        for file in sorted((out / "csv").glob("*.csv")):
            archive.write(file, file.name)
        for name in ("records.json", "check-submission.txt", "title-responses.json", "device-responses.json",
                     "device-journal.jsonl"):
            if (out / name).is_file():
                archive.write(out / name, name)
        for name in ("criteria-plan.json", "qwen-raw.json"):
            if (out.parent / name).is_file():
                archive.write(out.parent / name, name)
    return result

"""Project source-confirmed photovoltaic records into requested comparison tables."""
import csv
import json
import re


EFFICIENCY = re.compile(r"\b(?:pce|кпд|power conversion efficiency|эффективност\w* преобразовани\w*)\b", re.I)
COMPARISON = re.compile(r"\b(?:сопостав\w*|сравн\w*|compare|comparison|efficiency\.csv)\b", re.I)
REQUEST_METRICS = {
    "PCE": (EFFICIENCY, "Efficiency.csv", "efficiency_status"),
    "Voc": (re.compile(r"\b(?:v_?oc|open.circuit voltage|напряжени\w* холостого хода)\b", re.I), "Voc.csv", "voc_status"),
    "Jsc": (re.compile(r"\b(?:j_?sc|short.circuit current density)\b", re.I), "Jsc.csv", "jsc_status"),
    "FF": (re.compile(r"\b(?:ff|fill.factor|коэффициент\w* заполнения)\b", re.I), "FF.csv", "ff_status"),
}

COLUMNS = (
    "publication_id", "result_id", "device_id", "device_label", "measurement_id",
    "metric_name", "metric_value", "metric_unit", "raw_value", "raw_unit", "aggregation",
    "spread_value", "spectrum", "light_intensity", "intensity_unit", "scan_direction", "temperature",
    "source_file", "source_page", "source_table", "source_row", "source_column", "source_fragment", "source_bbox",
)


def comparison_metrics(criteria):
    if not COMPARISON.search(criteria):
        return []
    return [metric for metric, (pattern, _, _) in REQUEST_METRICS.items() if pattern.search(criteria)]


def confirmed_cell(record):
    source = record["sources"]["value"]
    normalized = record["normalized"]
    return (record.get("extraction_status") == "extracted" and bool(record.get("sample"))
            and not record.get("ambiguity") and normalized["value"] not in ("", None)
            and bool(normalized["unit"]) and bool(source["text"]) and bool(source.get("bbox")))


def comparison_row(doc, record):
    context = record["context"]
    normalized = record["normalized"]
    source = record["sources"]["value"]
    pin = context.get("pin", {})
    return {
        "publication_id": doc["publication_id"], "result_id": record["result_id"],
        "device_id": record["device_id"], "device_label": record["sample"],
        "measurement_id": record["measurement_id"], "metric_name": record["metric"],
        "metric_value": normalized["value"], "metric_unit": normalized["unit"],
        "raw_value": record["value_raw"], "raw_unit": record["unit_raw"],
        "aggregation": context.get("aggregation", {}).get("value", ""),
        "spread_value": normalized.get("spread_value", ""),
        "spectrum": context.get("spectrum", {}).get("value", ""),
        "light_intensity": pin.get("value", ""), "intensity_unit": pin.get("unit", ""),
        "scan_direction": context["scan_direction"], "temperature": context.get("temperature", {}).get("value", ""),
        "source_file": doc["file"], "source_page": record["page"], "source_table": record["table"],
        "source_row": record["sample"], "source_column": record["sources"]["header"]["text"],
        "source_fragment": source["text"], "source_bbox": json.dumps(source["bbox"], separators=(",", ":")),
    }


def write_comparison(export, documents, metric, paths, warnings):
    from .pce_text import find_pce_in_text

    _, table_name, status_column = REQUEST_METRICS[metric]
    articles = {path.name: path for path in paths}
    by_publication, comparison = {}, []
    for doc in documents:
        candidates = [row for row in doc["records"] if row["metric"] == metric]
        confirmed = [comparison_row(doc, row) for row in candidates if confirmed_cell(row)]
        coverage = doc["coverage"].get(metric, {})
        text_failed = False
        if metric == "PCE" and not confirmed and doc["file"] in articles:
            try:
                confirmed = [dict(row, publication_id=doc["publication_id"], source_file=doc["file"])
                             for row in find_pce_in_text(articles[doc["file"]])]
            except (OSError, ValueError, RuntimeError) as error:
                text_failed = True
                warnings.append(f"{doc['file']}. Текст для проверки КПД не прочитан. {error}")
        if confirmed:
            status = "confirmed"
        elif text_failed or coverage.get("status") == "processing_failed":
            status = "processing_failed"
        elif candidates or coverage.get("unresolved"):
            status = "needs_review"
        else:
            status = "not_found"
        by_publication[doc["publication_id"]] = status
        comparison.extend(confirmed)

    # Extend the already validated case export; leave the official template unchanged.
    publications = export / "publications.csv"
    with publications.open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        header = [*reader.fieldnames, status_column]
        rows = [dict(row, **{status_column: by_publication[row["publication_id"]]}) for row in reader]
    with publications.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)
    with (export / table_name).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(comparison)
    return {"table": table_name, "row_count": len(comparison), "publication_status": by_publication}

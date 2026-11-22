"""Index physical PDF pages from the evidence already saved by a completed job."""
import csv
import json
from collections import defaultdict
from pathlib import Path

from jobs import MAX_OUTPUT, SOLAR_TEMPLATE_CSV


LABELS = {"methods": "Методика", "illumination": "Освещение", "context": "Условия и пояснения",
          "values": "Значения", "records": "Записи по критериям"}


def page_number(value):
    text = str(value)
    return int(text) if text.isascii() and text.isdecimal() and 0 < len(text) < 9 and int(text) > 0 else None


def clean(value):
    return " ".join(str(value).split()) if isinstance(value, (str, int, float)) else ""


def matches_source(row, source_name):
    if row.get("source_file"):
        return row["source_file"] == source_name
    return row.get("publication_id") == Path(source_name).stem


def display_measurement(row, raw):
    sources = row.get("sources") if isinstance(row.get("sources"), dict) else {}
    cell = clean((sources.get("cell") or {}).get("text"))
    normalized = row.get("normalized")
    if isinstance(normalized, dict) and normalized.get("status") == "normalized":
        value, spread, unit = (clean(normalized.get(field)) for field in ("value", "spread_value", "unit"))
        has_spread = bool(clean(row.get("spread_raw")) or "±" in cell)
        if value and unit and (spread or not has_spread):
            return f"{value} ± {spread} {unit}" if spread else f"{value} {unit}"
    if cell:
        return f"{cell} {clean(row.get('unit_raw'))}".strip()
    # CSV outputs already pair metric_value and metric_unit with their exported spread.
    value = clean(row.get("metric_value"))
    spread = clean(row.get("spread_value") if value else row.get("spread_raw"))
    unit = clean(row.get("metric_unit") if value else row.get("unit_raw") or row.get("raw_unit"))
    number = value or raw
    return (f"{number} ± {spread} {unit}" if spread else f"{number} {unit}").strip()


class Findings:
    def __init__(self):
        self.marks = defaultdict(dict)

    def add(self, page, kind, key, detail):
        page = page_number(page)
        if not page:
            return
        group = self.marks[page].setdefault(kind, {"keys": set(), "details": []})
        if key in group["keys"]:
            return
        group["keys"].add(key)
        if detail:
            group["details"].append(detail)

    def numeric(self, row):
        page = row.get("source_page") or row.get("page")
        name = clean(row.get("metric_name") or row.get("metric"))
        value = next((number for key in ("raw_value", "value_raw", "metric_value")
                      if (number := clean(row.get(key)))), "")
        if not (name and value):
            return
        table = clean(row.get("source_table") or row.get("table"))
        sample = clean(row.get("sample") or row.get("device_label") or row.get("device_id"))
        context = row.get("context") if isinstance(row.get("context"), dict) else {}
        aggregation = clean((context.get("aggregation") or {}).get("value") or row.get("aggregation"))
        aggregation = {"mean": "среднее", "champion": "максимум", "maximum": "максимум"}.get(aggregation, aggregation)
        detail = " · ".join(filter(None, (table, sample, f"{name} {display_measurement(row, value)}", aggregation)))
        key = row.get("result_id") or (table, sample, name, value, clean(row.get("measurement_id")),
                                        clean(row.get("aggregation")), clean(row.get("source_fragment")))
        self.add(page, "values", key, detail)

    def context(self, row):
        context = row.get("context") or {}
        for field, item in context.items():
            if not isinstance(item, dict) or field in ("ambiguities", "illumination_link"):
                continue
            if field == "aggregation" and item.get("scope") in ("row", "column"):
                # Its printed header is already represented by the numeric cell on this page.
                continue
            kind = ("illumination" if field in ("pin", "spectrum", "insolation")
                    else "methods" if field in ("temperature", "fabrication") else "context")
            sources = [item.get("source") or item]
            if field == "fabrication" and isinstance(item.get("annealing_temperature"), dict):
                sources.append(item["annealing_temperature"].get("source"))
            for source in sources:
                if not isinstance(source, dict):
                    continue
                quote = clean(source.get("text"))
                if quote:
                    position = source.get("start")
                    if position is None:
                        position = str(source.get("bbox", ""))
                    self.add(source.get("page"), kind, (quote, position), quote)

    def pages(self):
        pages = []
        for page, kinds in sorted(self.marks.items()):
            marks = [{"kind": kind, "label": label, "count": len(group["keys"]), "details": group["details"]}
                     for kind, label in LABELS.items() if (group := kinds.get(kind))]
            pages.append({"page": page, "marks": marks})
        return pages


def findings_for_source(output_dir, source_name):
    directory = Path(output_dir)
    found = Findings()
    warnings = []
    records_file = directory / "records.json"
    if records_file.is_file():
        try:
            if records_file.is_symlink() or records_file.stat().st_size > MAX_OUTPUT:
                raise ValueError
            data = json.loads(records_file.read_text(encoding="utf-8-sig"))
            documents = [doc for doc in data.get("documents", []) if doc.get("file") == source_name]
            if documents:
                rows = (row for doc in documents for row in doc.get("records", [])
                        if row.get("source_file", source_name) == source_name)
            else:
                rows = (row for row in data.get("records", []) if matches_source(row, source_name))
            for row in rows:
                found.numeric(row)
                found.context(row)
        except (AttributeError, KeyError, TypeError, ValueError, OSError):
            warnings.append("Часть привязок из records.json недоступна. Карта дополнена по CSV.")

    for path in sorted(directory.glob("*.csv")):
        if path.name in SOLAR_TEMPLATE_CSV and path.name != "results.csv":
            continue
        try:
            if path.is_symlink() or path.stat().st_size > MAX_OUTPUT:
                raise ValueError
            with path.open(encoding="utf-8-sig", newline="") as stream:
                rows = csv.DictReader(stream)
                columns = rows.fieldnames or ()
                if "source_page" not in columns:
                    continue
                for index, row in enumerate(rows):
                    if not matches_source(row, source_name):
                        continue
                    if "metric_name" in columns and "metric_value" in columns:
                        found.numeric(row)
                    elif quote := clean(row.get("source_fragment")):
                        detail = f"{path.stem} · {quote}"
                        found.add(row["source_page"], "records", (path.name, index), detail)
        except (OSError, ValueError, csv.Error):
            warnings.append(f"Не удалось прочитать привязки из {path.name}.")
    return {"source_file": source_name, "pages": found.pages(), "warnings": warnings}

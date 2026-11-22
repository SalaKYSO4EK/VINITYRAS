"""Stable identifiers, table views and compact exports of published CSV rows."""

import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from itertools import islice


ROW_ID = re.compile(r"[0-9a-f]{32}")
SELECTED_COLUMNS = ("образец", "значение", "единица измерения", "страница", "цитата")
KEY_COLUMNS = {
    "publications.csv": ("publication_id",),
    "devices.csv": ("publication_id", "device_id"),
    "materials.csv": ("publication_id", "material_id"),
    "layers.csv": ("publication_id", "layer_id"),
    "measurements.csv": ("publication_id", "measurement_id"),
    "results.csv": ("publication_id", "result_id"),
    "issues.csv": ("publication_id", "issue_id"),
    **dict.fromkeys(("efficiency.csv", "voc.csv", "jsc.csv", "ff.csv"), ("publication_id", "result_id")),
}
TABLE_VALUES = {
    "devices.csv": ("active_area", "area_unit"),
    "materials.csv": ("bandgap_value", "bandgap_unit"),
    "layers.csv": ("layer_thickness", "thickness_unit"),
    "measurements.csv": ("light_intensity", "intensity_unit"),
}
VALUE_COLUMNS = (("metric_value", "metric_unit"), ("normalized_value", "normalized_unit"),
                 ("value", "unit"), ("raw_value", "raw_unit"))
SAMPLE_COLUMNS = ("device_label", "source_sample", "sample", "sample_label")
GENERIC_METADATA = {
    "publication_id", "device_id", "material_id", "measurement_id", "result_id", "source_file", "source_page",
    "source_fragment", "source_table", "source_row", "source_column", "source_bbox", "source_conditions",
    "conditions", "measurement_conditions", "metric_name", "verification_status", "unit", *SAMPLE_COLUMNS,
}
NUMERIC_SORT = re.compile(r"[+-]?(?:[0-9]{1,80}(?:[.,][0-9]{1,80})?|[.,][0-9]{1,80})(?:[eE][+-]?[0-9]{1,6})?")


@dataclass(frozen=True)
class TableQuery:
    offset: int
    limit: int
    sort_column: str = ""
    descending: bool = False
    filter_text: str = ""
    filter_column: str = ""


def read_csv(path):
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        yield from csv.DictReader(stream, strict=True)


def indexed_rows(path, job_id):
    """Identify rows in physical CSV order before filtering, sorting or slicing."""
    table = path.name.casefold()
    keys = KEY_COLUMNS.get(table, ())
    scope = hashlib.sha256(f"{job_id}\0{table}\0".encode("utf-8"))
    duplicates = Counter()
    for row in read_csv(path):
        if keys and all(row.get(key, "").strip() for key in keys):
            identity = ["primary", *(row[key] for key in keys)]
        else:
            identity = ["content", sorted(row.items())]
        digest = scope.copy()
        digest.update(json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        base = digest.digest()
        occurrence = duplicates[base]
        duplicates[base] += 1
        digest.update(f"\0{occurrence}".encode("ascii"))
        yield digest.hexdigest()[:32], row


def sort_value(value):
    value = value.strip()
    number = value.replace("−", "-")
    if NUMERIC_SORT.fullmatch(number):
        return 0, Decimal(number.replace(",", "."))
    return 1, value.casefold()


def view_rows(path, job_id, query):
    entries = indexed_rows(path, job_id)
    if query.filter_text:
        text = query.filter_text.casefold()
        column = query.filter_column
        entries = (entry for entry in entries if any(
            text in value.casefold() for value in ([entry[1][column]] if column else entry[1].values())
        ))
    if not query.sort_column:
        return entries
    populated, empty = [], []
    for entry in entries:
        (populated if entry[1][query.sort_column].strip() else empty).append(entry)
    populated.sort(key=lambda entry: sort_value(entry[1][query.sort_column]), reverse=query.descending)
    populated.extend(empty)
    return populated


def page_view(path, job_id, query, unfiltered_total):
    total = unfiltered_total
    if query.filter_text:
        selected = []
        total = 0
        for index, entry in enumerate(view_rows(path, job_id, query)):
            if query.offset <= index < query.offset + query.limit:
                selected.append(entry)
            total += 1
    elif query.offset >= total:
        selected = []
    else:
        selected = list(islice(view_rows(path, job_id, query), query.offset, query.offset + query.limit))
    return {"rows": [row for _, row in selected], "row_ids": [row_id for row_id, _ in selected],
            "offset": query.offset, "total": total}


def locate_row(path, job_id, query, unfiltered_total, row_id):
    total = unfiltered_total if not query.filter_text else 0
    found = None
    for index, (key, row) in enumerate(view_rows(path, job_id, query)):
        if query.filter_text:
            total += 1
        if key == row_id:
            found = {"row_id": row_id, "row": row, "index": index, "offset": index // query.limit * query.limit}
            if not query.filter_text:
                break
    if found is not None:
        found["total"] = total
    return found


def select_rows(paths, job_id, selected):
    wanted = defaultdict(set)
    for table, row_id in selected:
        wanted[table].add(row_id)
    found = {}
    for table, ids in wanted.items():
        for row_id, row in indexed_rows(paths[table], job_id):
            if row_id in ids:
                found[table, row_id] = row
                ids.remove(row_id)
                if not ids:
                    break
        if ids:
            return None
    return [(table, found[table, row_id]) for table, row_id in selected]


def related_values(path, identifiers, id_column, value_column):
    if path is None or not identifiers:
        return {}
    values = {}
    for row in read_csv(path):
        key = row.get("publication_id", ""), row.get(id_column, "")
        if key not in identifiers or not row.get(value_column, "").strip():
            continue
        value = row[value_column]
        values[key] = "" if key in values and values[key] != value else value
    return values


def sample_names(rows, paths):
    missing = [row for _, row in rows if not any(row.get(column, "").strip() for column in SAMPLE_COLUMNS)]
    measurement_keys = {(row["publication_id"], row["measurement_id"]) for row in missing
                        if row.get("publication_id") and row.get("measurement_id") and not row.get("device_id")}
    measured_devices = related_values(paths.get("measurements.csv"), measurement_keys, "measurement_id", "device_id")
    devices = {
        (row["publication_id"], row.get("device_id") or measured_devices.get((row["publication_id"], row.get("measurement_id"))))
        for row in missing if row.get("publication_id")
    }
    names = related_values(paths.get("devices.csv"), devices, "device_id", "device_label")
    return names, measured_devices


def exported_value(table, row):
    known = TABLE_VALUES.get(table)
    for column, unit in (*VALUE_COLUMNS, *((known,) if known else ())):
        if column == "raw_value" and row.get("extraction_status") not in (None, "", "extracted"):
            continue
        if row.get(column, "").strip():
            return row[column], row.get(unit, "")
    if table not in KEY_COLUMNS:
        facts = [column for column in row if column not in GENERIC_METADATA and not column.endswith("_unit")]
        if len(facts) == 1:
            field = facts[0]
            return row[field], row.get(f"{field}_unit") or row.get("unit", "")
    return "", ""


def write_selected_csv(stream, selected, paths):
    writer = csv.writer(stream)
    writer.writerow(SELECTED_COLUMNS)
    names, measured_devices = sample_names(selected, paths)
    for table, row in selected:
        sample = next((row[col] for col in SAMPLE_COLUMNS if row.get(col, "").strip()), "")
        if not sample:
            publication = row.get("publication_id", "")
            device = row.get("device_id") or measured_devices.get((publication, row.get("measurement_id")))
            sample = names.get((publication, device), "")
        value, unit = exported_value(table.casefold(), row)
        page = row.get("source_page", "")
        if not re.fullmatch(r"[1-9][0-9]*", page):
            page = ""
        writer.writerow((sample, value, unit, page, row.get("source_fragment", "")))

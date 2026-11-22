"""Compare a job to the checked photovoltaic export, using PDF identity and CSV values."""
from collections import defaultdict
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import unicodedata


REPORT_FILE = "reference-comparison.json"
REFERENCE = Path(__file__).resolve().parent / "reference" / "solar-test"
IDS = {
    "publications.csv": "publication_id", "materials.csv": "material_id",
    "devices.csv": "device_id", "layers.csv": "layer_id", "measurements.csv": "measurement_id",
    "results.csv": "result_id", "issues.csv": "issue_id",
}
LINKS = {
    "material_id": "materials.csv", "absorber_material_id": "materials.csv",
    "device_id": "devices.csv", "measurement_id": "measurements.csv",
    "duplicate_of": "results.csv", "record_ids": "results.csv",
}
NUMBER_FIELDS = {
    "year", "active_area", "layer_order", "layer_thickness", "light_intensity", "bandgap_value",
    "metric_value", "raw_value", "normalized_value", "spread_value", "source_page", "n_devices",
    "time_point", "annealing_temperature",
}
KEY_FIELDS = {
    "publications.csv": (), "materials.csv": ("material_name_raw",),
    "devices.csv": ("device_label",), "layers.csv": ("device_id", "layer_order"),
    "measurements.csv": ("device_id", "measurement_type", "scan_direction"),
    "results.csv": ("device_id", "metric_name", "aggregation", "source_page", "source_table", "time_point"),
    "issues.csv": ("check_name", "record_ids", "source_page"),
}
# A presentation note can contain prose, a duplicate source, or a translated label.
UNCOMPARED = {"note"}
EXAMPLES = 12


def file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def value(field, raw):
    text = " ".join(unicodedata.normalize("NFKC", raw or "").split())
    if field in NUMBER_FIELDS and re.fullmatch(r"[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)", text) and len(text) <= 80:
        try:
            number = Decimal(text.replace(",", "."))
            canonical = format(number, "f") if number else "0"
            return canonical.rstrip("0").rstrip(".") if "." in canonical else canonical
        except InvalidOperation:
            pass
    if field == "source_table":
        return re.sub(r"\s+", "", text).casefold()
    if field == "publication_id":
        return text.upper()
    return text


def table_data(files, pdf_ids):
    data = {}
    for name, path in files.items():
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            rows = [row for row in reader if value("publication_id", row.get("publication_id")) in pdf_ids]
            data[name] = reader.fieldnames or [], rows
    return data


def row_index(data):
    index = {}
    for name, (_, rows) in data.items():
        key_column = IDS[name]
        for row in rows:
            row_id = row.get(key_column)
            if row_id:
                key = name, value("publication_id", row.get("publication_id")), row_id.strip()
                index[key] = None if key in index else row
    return index


def linked(field, row, index):
    raw = row.get(field, "")
    if not raw.strip():
        return ""
    target = LINKS[field]
    pid = value("publication_id", row.get("publication_id"))
    keys = KEY_FIELDS[target]
    if target == "results.csv":
        keys = (*keys, "metric_value", "metric_unit", "raw_value")
    if target == "materials.csv":
        keys = ("material_name_raw", "formula_normalized")
    linked_values = []
    for row_id in raw.split(";"):
        ref = index.get((target, pid, row_id.strip()))
        if ref is None:
            linked_values.append(f"[ссылка не найдена: {row_id.strip()}]")
            continue
        parts = [linked(k, ref, index) if k in LINKS else value(k, ref.get(k)) for k in keys]
        if not any(parts):
            linked_values.append(f"[связь не подтверждена: {row_id.strip()}]")
        else:
            linked_values.append(" / ".join(parts))
    return " ; ".join(sorted(linked_values))


def groups(table, rows, columns, index):
    keys = KEY_FIELDS[table]
    grouped = defaultdict(list)
    for row in rows:
        fields = {*columns, *keys, "publication_id"}
        normalized = {}
        for field in fields:
            normalized[field] = (linked(field, row, index) if field in LINKS else value(field, row.get(field)))
        key = tuple(normalized[k] for k in ("publication_id", *keys))
        grouped[key].append(tuple(normalized[k] for k in columns))
    return grouped


def example(columns, values):
    return {k: v[:350] for k, v in zip(columns, values) if v}


def compare_table(name, generated, reference, current_index, reference_index):
    current_fields, current = generated
    reference_fields, expected = reference
    ignored = UNCOMPARED | ({IDS[name]} if name != "publications.csv" else set())
    columns = [c for c in reference_fields if c in current_fields and c not in ignored]
    info = {"current_rows": len(current), "reference_rows": len(expected),
            "compared_columns": columns, "extra_columns": [c for c in current_fields if c not in reference_fields],
            "reference_only_columns": [c for c in reference_fields if c not in current_fields]}
    if "publication_id" not in current_fields:
        return dict(info, status="schema_mismatch")
    if not current or not expected:
        return dict(info, status="empty_current" if not current else "empty_reference")
    if columns == ["publication_id"]:
        return dict(info, status="schema_mismatch")
    actual = groups(name, current, columns, current_index)
    gold = groups(name, expected, columns, reference_index)
    counts = dict(matches=0, changed=0, only_current=0, only_reference=0, ambiguous_groups=0)
    changes = []

    def record(kind, key, **data):
        if len(changes) < EXAMPLES:
            changes.append(dict(kind=kind, key=[part[:180] for part in key], **data))

    for key in sorted(actual.keys() | gold.keys()):
        left = list(actual.get(key, ()))
        right = list(gold.get(key, ()))
        unmatched = []
        for observation in left:
            if observation in right:
                right.remove(observation)
                counts["matches"] += 1
            else:
                unmatched.append(observation)
        if len(unmatched) == len(right) == 1:
            differences = {k: {"current": a[:350], "reference": b[:350]}
                           for k, a, b in zip(columns, unmatched[0], right[0]) if a != b}
            counts["changed"] += 1
            record("changed", key, fields=differences)
            continue
        if unmatched and right:
            counts["ambiguous_groups"] += 1
        counts["only_current"] += len(unmatched)
        counts["only_reference"] += len(right)
        for row in unmatched:
            record("only_current", key, row=example(columns, row))
        for row in right:
            record("only_reference", key, row=example(columns, row))
    status = "different" if counts["changed"] or counts["only_current"] or counts["only_reference"] else "match"
    return dict(info, status=status, **counts, examples=changes)


def compare(input_dir, output, reference=None):
    reference = REFERENCE if reference is None else reference
    manifest_path = reference / "manifest.json"
    if not manifest_path.is_file():
        if reference.exists():
            raise ValueError("Missing solar reference manifest")
        return None
    manifest = json.loads(manifest_path.read_bytes())
    if (manifest["schema_version"] != 1 or set(manifest["tables"]) != set(IDS)
            or not manifest["pdfs"]):
        raise ValueError("Unexpected solar reference manifest")
    pdf_hashes = {name.casefold(): digest for name, digest in manifest["pdfs"].items()}
    if len(pdf_hashes) != len(manifest["pdfs"]):
        raise ValueError("Duplicate PDF names in solar reference manifest")
    eligible = []
    others = []
    for path in sorted(input_dir.glob("*")):
        if path.suffix.casefold() != ".pdf":
            continue
        expected_hash = pdf_hashes.get(path.name.casefold())
        if expected_hash and path.is_file() and not path.is_symlink() and file_hash(path) == expected_hash:
            eligible.append(path.name)
        else:
            others.append(path.name)
    if not eligible:
        return None

    ref_files = {name: reference / name for name in IDS}
    if any(file_hash(path) != manifest["tables"][name] for name, path in ref_files.items()):
        raise ValueError("Solar reference CSV failed checksum")
    current_files = {p.name.casefold(): p for p in output.glob("*")
                     if p.name.casefold() in IDS and p.is_file() and not p.is_symlink()}
    pdf_ids = {Path(name).stem.upper() for name in eligible}
    current, expected = table_data(current_files, pdf_ids), table_data(ref_files, pdf_ids)
    current_index, reference_index = row_index(current), row_index(expected)
    tables = {name: compare_table(name, current[name], expected[name], current_index, reference_index)
              for name in current}
    return {"schema_version": 1, "reference": manifest["revision"], "matched_files": eligible,
            "other_files": others, "tables": tables, "ignored_columns": sorted(UNCOMPARED),
            "example_limit_per_table": EXAMPLES,
            "absent_tables": [name for name in IDS if name not in tables]}

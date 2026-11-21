"""Project a criterion-specific model plan onto an explicitly selected table configuration."""
from __future__ import annotations

from copy import deepcopy
import csv
import json
from pathlib import Path
import re

from .criteria import FRAGMENT_FIELD, SOURCE_FIELDS


def apply_schema_config(profile: dict, path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    template_dir = (path.parent / cfg["template_directory"]).resolve()
    tables = {}
    for name, definition in cfg["tables"].items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name):
            raise ValueError(f"Invalid schema table name: {name}")
        with (template_dir / f"{name}.csv").open(encoding="utf-8-sig", newline="") as stream:
            columns = next(csv.reader(stream))
        for key in [definition["primary_key"], *(ref["columns"] for ref in definition["references"])]:
            if not set(key) <= set(columns):
                raise ValueError(f"Reference or key outside template table: {name}: {key}")
        tables[name] = {**definition, "columns": columns, "criteria_groups": []}

    result = deepcopy(profile)
    extraction = result["extraction"]
    original = extraction["groups"]
    groups: dict[str, dict] = {}
    mapping = []
    ambiguous = []
    pending = []
    for group in original:
        name = group["name"]
        fields = {field["name"] for field in group["fields"]}
        scope = next((item for item in cfg["scope_only"] if name == item["name"]
                      and fields == set(item["fields"])), None)
        if scope:
            mapping.append({"group": name, "status": "scope_only", **scope})
            continue
        options = [item for item in cfg["groups"] if name in item["names"] and set(item["has"]) <= fields]
        if len(options) > 1:
            ambiguous.append({"group": name, "options": [item["target"] for item in options],
                              "status": "needs_clarification"})
            pending.append(group)
            continue
        if not options:
            # Preserve criteria-specific groups which have no configured table mapping.
            pending.append(group)
            continue
        route = options[0]
        target = route["target"]
        status = {"group": name, "table": target, "role": route["role"],
                  "criteria_page": route["criteria_page"], "status": "mapped"}
        if route.get("companion"):
            status.update(row_key=tables[target]["primary_key"], row_join_status="requires_same_result_evidence")
        mapping.append(status)
        tables[target]["criteria_groups"].append(name)
        canonical = groups.setdefault(target, {"name": target, "row": group["row"], "fields": []})
        if not route.get("companion"):
            canonical["row"] = group["row"]
        used = {f["name"] for f in canonical["fields"]}
        canonical["fields"].extend(f for f in group["fields"] if f["name"] not in used)

    for group in pending:
        name = group["name"]
        if name in groups:
            ambiguous.append({"group": name, "options": [name], "status": "needs_clarification",
                              "reason": "Custom group collides with a canonical table"})
            # Retain a separate group and keep the original name in the audit.
            suffix = 2
            while f"{name}_{suffix}" in groups:
                suffix += 1
            group = {**group, "name": f"{name}_{suffix}"}
        groups[group["name"]] = group

    strict = extraction.get("exact_model_fields", False)
    extraction["groups"] = list(groups.values())
    extraction["nuextract_template"] = {}
    for name, group in groups.items():
        columns = [field["name"] for field in group["fields"] if field["name"] not in SOURCE_FIELDS]
        if not strict and FRAGMENT_FIELD not in columns:
            columns.append(FRAGMENT_FIELD)
        extraction["nuextract_template"][name] = [dict.fromkeys(columns, "")]
    for name, table in tables.items():
        included = {field["name"] for field in groups.get(name, {}).get("fields", [])}
        table["template_only_columns"] = [column for column in table["columns"] if column not in included]
    result["submission_schema"] = {
        "configuration": cfg["name"],
        "origin_groups": original,
        "mapping": mapping,
        "unmapped_groups": [group["name"] for group in pending],
        "unresolved": ambiguous,
        "tables": tables,
    }
    return result

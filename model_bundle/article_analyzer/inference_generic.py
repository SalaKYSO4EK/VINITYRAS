"""Export criterion-specific NuExtract groups with physical-page evidence."""
import csv
from copy import deepcopy
from pathlib import Path, PureWindowsPath
import re

import pdfplumber

from .criteria import FRAGMENT_FIELD, SOURCE_FIELDS, Source, extract_with_profile
from .local_model import LocalNuExtract
from .visible_pdf import visible_page

SOURCE = ("publication_id", "source_file", "source_page", "source_fragment", "source_table",
          "source_row", "source_column", "source_bbox", "source_sample", "source_conditions")


def flat(text):
    return " ".join(str(text or "").split())


def page_cells(page):
    cells = []
    for table in page.find_tables():
        grid = table.extract()
        if len(grid) < 2 or not grid[0]:
            continue
        top = table.bbox[1]
        nearby = [w for w in page.extract_words() if 0 < top - w["bottom"] < 64]
        caption = re.findall(r"(?:Table|Таблица)\s+[A-Z]?\d+[A-Za-z]?", " ".join(w["text"] for w in nearby), re.I)
        for values, row in zip(grid[1:], table.rows[1:]):
            for column in range(1, len(values)):
                text, box = flat(values[column]), row.cells[column]
                if not (text and box and flat(values[0]) and flat(grid[0][column])):
                    continue
                cells.append((text, {"source_table": caption[-1] if caption else "",
                                     "source_column": flat(grid[0][column]), "source_row": flat(values[0]),
                                     "source_bbox": ",".join(f"{v:.2f}" for v in box)}))
    return cells


def export_group_item(record, check, path, number, cells):
    quote = record.get("source_fragment", "")
    facts = {k: v for k, v in record.items() if k not in (*SOURCE, FRAGMENT_FIELD)}
    if not any(facts.values()) or not quote or not check["quote_found_on_page"]:
        return None
    row = dict(facts, publication_id=path.stem, source_file=path.name, source_page=number, source_fragment=quote)
    for name, candidates in {"source_sample": ("source_sample", "sample", "sample_label", "device_label"),
                             "source_conditions": ("source_conditions", "measurement_conditions", "conditions")}.items():
        values = {record[k] for k in candidates if record.get(k)}
        if len(values) == 1:
            row[name] = values.pop()
    context = {flat(v) for v in (*facts.values(), record.get("source_row"), record.get("source_column"),
                                 record.get("source_sample")) if v}
    matches = [location for value, location in cells if value in context and
               (location["source_row"] in context or location["source_column"] in context)]
    if len(matches) > 1:
        matches = [m for m in matches if m["source_row"] in context and m["source_column"] in context]
    if len(matches) == 1:
        row.update(matches[0])
    else:
        for key in ("source_table", "source_row", "source_column"):
            if record.get(key):
                row[key] = record[key]
    return row


def run_generic(paths, plan, export, warnings):
    profile = deepcopy(plan)
    extraction = profile["extraction"]
    groups = extraction["groups"]
    templates = extraction["nuextract_template"]
    headers, rows, aliases = {}, {}, {}
    for group in groups:
        name = group["name"]
        base = name
        while PureWindowsPath(base + ".csv").is_reserved() or base.casefold() in aliases.values():
            base = "group_" + base
        aliases[name] = base.casefold()
        fields = [f["name"] for f in group["fields"] if f["name"] not in (*SOURCE, FRAGMENT_FIELD)]
        headers[name] = [*SOURCE[:1], *fields, *SOURCE[1:]]
        rows[name] = []
        for administrative in ("publication_id", "source_file", "source_bbox"):
            templates[name][0].pop(administrative, None)
    warnings.append("В общем профиле цитата подтверждает текст на странице. Смысл связи и правила предметной области требуют отдельной сверки.")
    reports = []
    model = LocalNuExtract(Path(__file__).resolve().parents[1] / "models/NuExtract-1.5-tiny")
    for path in paths:
        start_count = sum(map(len, rows.values()))
        with pdfplumber.open(path) as pdf:
            for number, original in enumerate(pdf.pages, 1):
                try:
                    page = visible_page(original)
                    text = page.extract_text() or ""
                    if not text.strip():
                        warnings.append(f"{path.name}, стр. {number}. Нет извлекаемого текста для NuExtract.")
                        continue
                    article = Source(path.name, "", {number: text})
                    report = extract_with_profile(model, profile, article, None, 512)
                except (ValueError, OSError, RuntimeError) as error:
                    warnings.append(f"{path.name}, стр. {number}. Обработка страницы ограничена. {error}")
                    continue
                cells = []
                if any(group.get("records") for group in report["groups"].values()):
                    try:
                        cells = page_cells(page)
                    except (ValueError, OSError, RuntimeError) as error:
                        warnings.append(f"{path.name}, стр. {number}. Не удалось подтвердить геометрию таблицы. {error}")
                reports.append({"file": path.name, "page": number, "groups": report["groups"]})
                for name, group in report["groups"].items():
                    if group.get("error"):
                        warnings.append(f"{path.name}, стр. {number}, {name}. NuExtract не вернула полную запись. {group['error']}")
                    for record, check in zip(group.get("records", []), group.get("citation_checks", [])):
                        row = export_group_item(record, check, path, number, cells)
                        if row:
                            rows[name].append(row)
                        elif any(record.get(f) for f in headers[name] if f not in SOURCE):
                            warnings.append(f"{path.name}, стр. {number}, {name}. Для строки не подтверждён единый фрагмент.")
        if sum(map(len, rows.values())) == start_count:
            warnings.append(f"{path.name}. По выбранным группам не найдено строк с подтверждённой цитатой.")
    for name in headers:
        with (export / (aliases[name] + ".csv")).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=headers[name])
            writer.writeheader()
            writer.writerows(rows[name])
    return {"groups": aliases, "rows": {name: len(values) for name, values in rows.items()},
            "pages": reports}


"""Export criterion-specific NuExtract groups with physical-page evidence."""
import csv
from collections import Counter
from copy import deepcopy
from pathlib import Path, PureWindowsPath
import re

import pdfplumber
from pdfplumber.utils.exceptions import PdfminerException

from .criteria import FRAGMENT_FIELD, Source, extract_with_profile
from .evidence import EvidenceIndex, render_fragments
from .generic_consensus import COUNTS, STATUS, flat, verify_group
from .job_progress import report_phase
from .local_model import LocalNuExtract
from .visible_pdf import visible_page

SOURCE = ("publication_id", "source_file", "source_page", "source_fragment", "source_table",
          "source_row", "source_column", "source_bbox", "source_sample", "source_conditions")


def page_cells(page):
    cells = []
    for table_id, table in enumerate(page.find_tables()):
        grid = table.extract()
        if len(grid) < 2 or not grid[0]:
            continue
        top = table.bbox[1]
        nearby = [w for w in page.extract_words() if 0 < top - w["bottom"] < 64]
        caption = re.findall(r"(?:Table|Таблица)\s+[A-Z]?\d+[A-Za-z]?", " ".join(w["text"] for w in nearby), re.I)
        for row_id, (values, row) in enumerate(zip(grid[1:], table.rows[1:])):
            for column in range(1, len(values)):
                text, box = flat(values[column]), row.cells[column]
                if not (text and box and flat(values[0]) and flat(grid[0][column])):
                    continue
                cells.append({"text": text, "row_id": (table_id, row_id),
                              "source_table": caption[-1] if caption else "",
                              "source_column": flat(grid[0][column]), "source_row": flat(values[0]),
                              "source_bbox": ",".join(f"{v:.2f}" for v in box)})
    return cells


class ReorderedEvidence:
    """A fresh extraction with a different prompt and the same source IDs and fields."""

    def __init__(self, model, evidence):
        self.model = model
        self.revision = model.revision
        self.fragments = dict(reversed(tuple(evidence.fragments.items())))

    def extract(self, prompt, template, **options):
        instructions, separator, article = prompt.partition("\n\nARTICLE\n")
        if not separator:
            raise ValueError("No source block in extraction prompt.")
        if article.startswith("[PAGE "):
            heading = article.split("\n", 1)[0]
            contents = "\n\n".join(fragment.text for fragment in self.fragments.values())
            presented = f"{heading}\n{contents}"
        else:
            presented = render_fragments(self.fragments)
        question = ("For tabular facts, check the row label together with the column header. "
                    "Keep a field empty if the link to this record is unclear.")
        return self.model.extract(f"{instructions}\n{question}{separator}{presented}", template, **options)


def extract_pass(model, profile, article):
    try:
        return extract_with_profile(model, profile, article, None, 512)
    except (ValueError, OSError, RuntimeError) as error:
        return {"groups": {}, "error": str(error)}


def custom_pages(path, warnings, limited_sources):
    try:
        with pdfplumber.open(path) as pdf:
            yield from enumerate(pdf.pages, 1)
    except (PdfminerException, OSError, ValueError) as error:
        limited_sources.add(path.name)
        warnings.append(f"{path.name}. Не удалось разобрать страницы для пользовательских критериев. {error}")


def run_generic(paths, plan, export, warnings):
    profile = deepcopy(plan)
    extraction = profile["extraction"]
    groups = extraction["groups"]
    templates = extraction["nuextract_template"]
    headers, fields, rows, aliases = {}, {}, {}, {}
    for group in groups:
        name = group["name"]
        base = name
        while PureWindowsPath(base + ".csv").is_reserved() or base.casefold() in aliases.values():
            base = "group_" + base
        aliases[name] = base.casefold()
        fields[name] = [f["name"] for f in group["fields"] if f["name"] not in (*SOURCE, FRAGMENT_FIELD, STATUS)]
        headers[name] = [*SOURCE[:1], *fields[name], *SOURCE[1:], STATUS]
        rows[name] = []
        for administrative in ("publication_id", "source_file", "source_bbox", STATUS):
            templates[name][0].pop(administrative, None)
    reports = []
    counts = Counter(dict.fromkeys((*COUNTS, "unanchored"), 0))
    confirmed_sources = []
    limited_sources = set()
    paths = list(paths)
    model = LocalNuExtract(Path(__file__).resolve().parents[1] / "models/NuExtract-1.5-tiny")
    for index, path in enumerate(paths, 1):
        report_phase("extract_custom", pdf_index=index, pdf_total=len(paths), file=path.name)
        start_count = sum(map(len, rows.values()))
        start_confirmed = counts["agreed"] + counts["resolved_from_source"]
        for number, original in custom_pages(path, warnings, limited_sources):
            try:
                page = visible_page(original)
                text = page.extract_text() or ""
                if not text.strip():
                    warnings.append(f"{path.name}, стр. {number}. Нет извлекаемого текста для NuExtract.")
                    continue
                article = Source(path.name, "", {number: text})
                evidence = EvidenceIndex(article.pages)
            except (PdfminerException, ValueError, OSError, RuntimeError) as error:
                limited_sources.add(path.name)
                warnings.append(f"{path.name}, стр. {number}. Обработка страницы ограничена. {error}")
                continue
            passes = {"first": extract_pass(model, profile, article),
                      "second": extract_pass(ReorderedEvidence(model, evidence), profile, article)}
            cells = []
            if any(group.get("records") for run in passes.values() for group in run["groups"].values()):
                try:
                    cells = page_cells(page)
                except (PdfminerException, ValueError, OSError, RuntimeError) as error:
                    limited_sources.add(path.name)
                    warnings.append(f"{path.name}, стр. {number}. Не удалось подтвердить геометрию таблицы. {error}")
            differences = {}
            for pass_name, run in passes.items():
                failures = [("", run["error"])] if run.get("error") else [
                    (name, group["error"]) for name, group in run["groups"].items() if group.get("error")
                ]
                for name, error in failures:
                    warnings.append(f"{path.name}, стр. {number}, {pass_name} {name}. Ошибка NuExtract: {error}")
            for name, requested in fields.items():
                if not requested:
                    continue
                first, second = (run["groups"].get(name, {}) for run in passes.values())
                if all(run.get("error") or run["groups"].get(name, {}).get("error") for run in passes.values()):
                    limited_sources.add(path.name)
                accepted, verification = verify_group(first, second, requested, path, number, evidence, cells)
                rows[name].extend(accepted)
                unchecked = sum(map(len, verification["unanchored"].values()))
                counts.update(row[STATUS] for row in accepted)
                counts["unanchored"] += unchecked
                if unchecked or accepted:
                    differences[name] = verification
                unresolved = unchecked + sum(row[STATUS] == "needs_review" for row in accepted)
                if unresolved:
                    warnings.append(f"{path.name}, стр. {number}, {name}. Требуют сверки записей: {unresolved}.")
            reports.append({"file": path.name, "page": number, "passes": passes, "verification": differences})
        if path.name not in limited_sources and sum(map(len, rows.values())) == start_count:
            warnings.append(f"{path.name}. По выбранным группам не найдено строк с подтверждённой цитатой.")
        if counts["agreed"] + counts["resolved_from_source"] > start_confirmed:
            confirmed_sources.append(path.name)
    for name in headers:
        with (export / (aliases[name] + ".csv")).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=headers[name])
            writer.writeheader()
            writer.writerows(rows[name])
    return {"groups": aliases, "rows": {name: len(values) for name, values in rows.items()},
            "confirmed_sources": confirmed_sources, "limited_sources": sorted(limited_sources),
            "pages": reports, "verification": {"method": "two_nuextract_passes", "counts": dict(counts)}}


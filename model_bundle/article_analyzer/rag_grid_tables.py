"""Evaluation tables with a lower caption and horizontal rules separating generator groups."""
from __future__ import annotations

import re

from .photovoltaic_tables import bounds, lines, mid, text
from .rag_tables import CAPTION, METRIC, MODEL, NUMBER
from .visible_pdf import visible_page


DATASET_LEAD = re.compile(
    r"^Table\s+(?:[A-Z]?\d+[a-z]?|[IVXLCDM]+)\s*[:.]\s*"
    r"(?P<dataset>[A-Za-z][\w.-]*(?:\s+[A-Z][\w.-]*){0,3})\s+"
    r"(?:answer|response|retrieval|question.answering|generation|evaluation|benchmark|performance)\b", re.I & ~re.I,
)
SCALAR = re.compile(NUMBER)
SPREAD = re.compile(rf"(?P<value>{NUMBER})\s*\(\s*±\s*(?P<spread>{NUMBER})\s*\)")


def in_band(words, top, bottom):
    return [w for w in words if top <= (w["top"] + w["bottom"]) / 2 < bottom]


def caption_below(words, edge):
    left, right, bottom = edge["x0"], edge["x1"], edge["top"]
    local = [w for w in words if left - 3 <= w["x0"] and w["x1"] <= right + 4
             and bottom <= w["top"] < bottom + 110]
    grouped = lines(local, tolerance=2.2)
    if not grouped or min(w["top"] for w in grouped[0]) - bottom > 20:
        return None
    first = text(grouped[0])
    match = CAPTION.match(first)
    dataset = DATASET_LEAD.match(first)
    if not match or not dataset:
        return None
    caption = first
    previous = grouped[0]
    for line in grouped[1:]:
        gap = min(w["top"] for w in line) - max(w["bottom"] for w in previous)
        if gap > 6 or CAPTION.match(text(line)):
            break
        caption += "\n" + text(line)
        previous = line
    return f"Table {match[1]}", dataset["dataset"], caption


def header_columns(words, caption):
    header = sorted(words, key=lambda w: w["x0"])
    recognized = []
    for word in header:
        role = word["text"]
        # An unfamiliar abbreviation is usable only when explicitly defined in this caption.
        defined = re.fullmatch(r"[A-Z][A-Z0-9-]{0,8}", role) and f"({role})" in caption
        if METRIC.fullmatch(role) or defined:
            recognized.append(word)
    if len(recognized) < 2 or len({w["text"] for w in recognized}) != len(recognized):
        return []
    start = header.index(recognized[0])
    if start == 0 or header[start:] != recognized or not any(c.isalpha() for c in text(header[:start])):
        return []
    return recognized


def extract_group(group, columns, left, right, number, table, dataset, caption):
    rows = lines(group, tolerance=2.2)
    if len(rows) < 3:
        return []
    generator_row = rows[0]
    generator = text(generator_row)
    if not MODEL.search(generator) or max(w["x1"] for w in generator_row) > columns[0]["x0"] + 3:
        return []
    centers = [mid(w) for w in columns]
    cuts = [centers[0] - (centers[1] - centers[0]) / 2]
    cuts.extend((a+b)/2 for a,b in zip(centers,centers[1:]))
    cuts.append(right + 1)
    records = []
    for row in rows[1:]:
        labels = [w for w in row if left - 1 <= mid(w) < cuts[0]]
        sample = text(labels)
        if not sample or not any(c.isalpha() for c in sample):
            continue
        cells = [[w for w in row if lo <= mid(w) < hi] for lo,hi in zip(cuts,cuts[1:])]
        # Require a full numeric row so prose and model labels cannot become observations.
        if any(not cell for cell in cells):
            continue
        parsed = []
        for idx, cell in enumerate(cells):
            cell = sorted(cell,key=lambda w:w["x0"])
            value = cell[0]
            if not SCALAR.fullmatch(value["text"]):
                continue
            raw = value["text"]
            metadata = {}
            if len(cell) > 1:
                sign = [w for w in group if w["text"] == "±" and cell[0]["x1"] <= mid(w) <= cell[-1]["x0"]
                        and value["top"] <= w["top"] < value["bottom"] and w not in cell]
                parts = cell + sign
                raw = text(parts)
                with_spread = SPREAD.fullmatch(raw)
                if not with_spread:
                    continue
                metadata = {"spread_raw":with_spread["spread"],"cell_fragment":raw,"cell_bbox":bounds(parts)}
            column = columns[idx]
            parsed.append({"page":number, "table":table, "sample":sample, "generator":generator,
                           "dataset":dataset, "metric":column["text"], "raw":raw, "unit":"",
                           "fragment":value["text"], "bbox":bounds([value]), "row_fragment":sample,
                           "row_bbox":bounds(labels), "header_fragment":column["text"],
                           "header_bbox":bounds([column]), "generator_bbox":bounds(generator_row),
                           "caption_fragment":caption, **metadata})
        if len(parsed) == len(columns):
            records.extend(parsed)
    return records


def extract_grid_cells(page, page_number):
    """Return cells only for tables with individually bounded, explicitly named model groups."""
    relevant = [edge for edge in page.horizontal_edges if edge["x1"]-edge["x0"]>=100]
    if len(relevant)<4:
        return []
    visible = visible_page(page)
    try:
        words = visible.extract_words(x_tolerance=1.1,y_tolerance=2)
    finally:
        if visible is not page:
            visible.close()
    output = []
    for floor in relevant:
        caption = caption_below(words, floor)
        if not caption:
            continue
        left,right,bottom = floor["x0"],floor["x1"],floor["top"]
        tops = sorted({round(e["top"],2) for e in relevant if abs(e["x0"]-left)<1.5
                       and abs(e["x1"]-right)<1.5 and bottom-380 <= e["top"]<=bottom})
        local = [w for w in words if left-1 <= mid(w)<=right+1]
        for i in range(len(tops)-3):
            top,head_end = tops[i:i+2]
            if not 5 <= head_end-top <= 32:
                continue
            heading = lines(in_band(local, top, head_end), tolerance=2.2)
            columns = header_columns(heading[0], caption[2]) if len(heading)==1 else []
            if not columns:
                continue
            groups = [extract_group(in_band(local,a,b),columns,left,right,page_number,*caption)
                      for a,b in zip(tops[i+1:],tops[i+2:])]
            if groups and all(groups):
                for group in groups:
                    output.extend(group)
                break
    return output

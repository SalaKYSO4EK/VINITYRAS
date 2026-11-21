"""Recover printed cells from segmented horizontal rules and merged labels."""
from __future__ import annotations

from collections import defaultdict
import re

from article_analyzer.photovoltaic_tables import (
    CAPTION, NUMBER, aggregation_cell, bounds, caption_above, inside, lines, metrics_in_header, number_lines, record, text,
)


NUMERIC_CELL = re.compile(r"\s*(?P<value>[+\-−]?\d+(?:[.,]\d+)*(?:\s*%)?)"
                          r"(?:\s*(?:±|\+\s*/\s*[-−])\s*(?P<spread>\d+(?:[.,]\d+)*)(?:\s*%)?)?\s*")


def dark_rule(edge):
    color = edge.get("stroking_color")
    # White strokes span merged cells in some PDFs.
    return color not in (1, (1, 1, 1), (1,)) and edge["x1"] - edge["x0"] >= 5


def line_source(words):
    if not words:
        return None
    rows = lines(words, tolerance=3)
    value = " ".join(text(row) for row in rows)
    value = re.sub(r"(?<=\d)\s+(?=[°◦º])", "", value)
    return {"text": value, "bbox": bounds(words)}


def row_limits(edges, x):
    values = []
    for edge in edges:
        if dark_rule(edge) and edge["x0"] + .5 < x < edge["x1"] - .5:
            y = edge["top"]
            if not values or y - values[-1] > 1:
                values.append(y)
    return values


def joined_rulings(edges):
    """Join visible line segments at the same height, keeping the actual grid edges separate."""
    levels = defaultdict(list)
    for edge in edges:
        if dark_rule(edge):
            levels[round(edge["top"], 1)].append((edge["x0"], edge["x1"]))
    for y, spans in sorted(levels.items()):
        ordered = sorted(spans)
        left, right = ordered[0]
        for start, stop in ordered[1:]:
            if start - right > .6:
                yield left, y, right
                left, right = start, stop
            else:
                right = max(right, stop)
        yield left, y, right


def full_width_regions(rulings, words, page_height):
    """Only consider ruled regions with an actual table caption and closing rule."""
    spans = defaultdict(list)
    for left, top, right in rulings:
        if right - left >= 115:
            spans[round(left, 1), round(right, 1)].append(top)
    for (x0, x1), ys in spans.items():
        if len(ys) < 2:
            continue
        previous_bottom = -1
        for top, lower in zip(ys, ys[1:]):
            if top <= previous_bottom or lower - top < 10:
                continue
            region = (x0, top, x1, page_height)
            caption = caption_above(words, region, top)
            if not CAPTION.match(caption["text"]):
                continue
            # A later caption separates tables with the same width on one page.
            bottom = ys[-1]
            for y in ys:
                if y <= lower:
                    continue
                following = caption_above(words, (x0, y, x1, page_height), y)
                if CAPTION.match(following["text"]) and following["bbox"][1] > top:
                    bottom = max(prior for prior in ys if prior < y)
                    break
            if bottom - top >= 45:
                yield (x0, top, x1, bottom), caption
                previous_bottom = bottom


def header_cut(rulings, box, first):
    """Separate a spanning header from its metric and unit cells."""
    x0, top, x1, _ = box
    cuts = [y for left, y, right in rulings if top + 1 < y < first and left <= x0 + .7 and right >= x1 - .7]
    return cuts[-2] if len(cuts) > 1 else top


def grid_columns(edges, box, first):
    x0, _, x1, bottom = box
    events = defaultdict(set)
    for e in edges:
        if first < e["top"] < bottom and x0 - 1 <= e["x0"] and e["x1"] <= x1 + 1:
            for x in (e["x0"], e["x1"]):
                if x0 + 5 < x < x1 - 5:
                    events[round(x * 2) / 2].add(round(e["top"], 1))
    internal = [x for x, ys in events.items() if len(ys) >= 2]
    xs = [x0, *sorted(internal), x1]
    return list(zip(xs, xs[1:]))


def words_in_column(words, x0, x1, top, bottom):
    return [w for w in words if x0 <= (w["x0"] + w["x1"]) / 2 < x1
            and top <= (w["top"] + w["bottom"]) / 2 < bottom]


def data_columns(words, columns, top, first):
    headers = {}
    for index, (x0, x1) in enumerate(columns):
        head_words = words_in_column(words, x0, x1, top, first)
        if any(w["text"] in {"×", "·", "*", "/"} for w in head_words):
            continue
        candidates = metrics_in_header(head_words, bounded_column=True)
        if len(candidates) == 1 and candidates[0]["unit"]:
            headers[index] = candidates[0]
    if len(headers) != len({h["metric"] for h in headers.values()}):
        return {}
    return headers


def sample_cells(words, columns, headers, lead_limits, body_top, middle, bottom):
    parts, aggregation = [], None
    for index, (x0, x1) in enumerate(columns):
        limits = lead_limits[index]
        start = max((y for y in limits if y <= middle), default=body_top)
        stop = min((y for y in limits if y > middle), default=bottom)
        part = line_source(words_in_column(words, x0, x1, start, stop))
        if not part:
            continue
        designation = aggregation_cell(part)
        if designation and index == len(columns) - 1:
            aggregation = designation
            continue
        heading = headers[index]
        label = part["text"].replace("◦", "°").replace("º", "°")
        unit = re.search(r"\((nm|[μµ]m|mm|cm)\)$", heading["text"]) if heading else None
        if unit and NUMBER.fullmatch(label):
            label += " " + unit[1]
        if re.fullmatch(r"[-–—−]+", label):
            label = ""
        parts.append(dict(part, header=heading, label=label, row_span=[round(start, 3), round(stop, 3)]))
    return parts, aggregation


def recover_rows(words, edges, box, caption, number):
    x0, top, x1, bottom = box
    body = number_lines(inside(words, box), x1 - x0)
    if not body:
        return []
    first = min(w["top"] for w in body[0][1])
    columns = grid_columns(edges, box, first)
    leaf_top = header_cut(joined_rulings(edges), box, first)
    headers = data_columns(words, columns, leaf_top, first - 2)
    if len(headers) < 3 and leaf_top > top:
        leaf_top = top
        headers = data_columns(words, columns, top, first - 2)
    if len(headers) < 3:
        return []
    lead = columns[:min(headers)]
    if len(lead) < 2:
        return []
    leader_heads = [line_source(words_in_column(words, lo, hi, leaf_top, first - 2)) for lo, hi in lead]
    upper_header = line_source(inside(words, (x0, top, x1, leaf_top))) if leaf_top > top else None
    limits = [row_limits(edges, (lo + hi) / 2) for lo, hi in columns]
    shared = set(round(y, 1) for y in limits[min(headers)] if top < y <= bottom + .2)
    for i in headers:
        shared.intersection_update(round(y, 1) for y in limits[i] if top < y <= bottom + .2)
    ys = sorted(shared)
    if len(ys) < 4 or not any(y <= first for y in ys):
        return []
    ys = [y for y in ys if y > first or y == max(t for t in ys if t <= first)]
    table = CAPTION.match(caption["text"])[0].strip(" .|:")
    recovered = []
    for start, stop in zip(ys, ys[1:]):
        middle = (start + stop) / 2
        parts, aggregation = sample_cells(words, lead, leader_heads, limits[:len(lead)],
                                          ys[0], middle, bottom)
        labels = [p["label"] for p in parts if p["label"]]
        if not labels or not aggregation:
            continue
        sample = " / ".join(labels)
        label_box = [min(p["bbox"][0] for p in parts), min(p["bbox"][1] for p in parts),
                     max(p["bbox"][2] for p in parts), max(p["bbox"][3] for p in parts)]
        for index, header in headers.items():
            col_words = words_in_column(words, *columns[index], start, stop)
            original = line_source(col_words)
            parsed = NUMERIC_CELL.fullmatch(original["text"]) if original else None
            if not parsed:
                continue
            value = dict(min(col_words, key=lambda w: w["x0"]), text=parsed["value"].strip())
            entry = record(number, table, header, sample, label_box, value, label_origin="merged_cells",
                           occurrence=round(middle, 3))
            entry["spread_raw"] = parsed["spread"] or ""
            entry["sources"].update(caption=caption, cell=original, sample_parts=parts, aggregation=aggregation,
                                    upper_header=upper_header)
            recovered.append(entry)
    return recovered


def merged_tables(page, number):
    edges = sorted((e for e in page.horizontal_edges if e["object_type"] == "line"), key=lambda e: e["top"])
    if len(edges) < 12:
        return
    words = page.extract_words(x_tolerance=1, y_tolerance=2)
    for box, caption in full_width_regions(joined_rulings(edges), words, page.height):
        local_edges = [e for e in edges if box[1] - .1 <= e["top"] <= box[3] + .1
                       and e["x0"] >= box[0] - .1 and e["x1"] <= box[2] + .1]
        records = recover_rows(words, local_edges, box, caption, number)
        if records:
            yield box, records

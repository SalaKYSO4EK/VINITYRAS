"""Recover PV tables from captions, aligned text, and explicit row group markers."""
from __future__ import annotations

import re

from article_analyzer.photovoltaic_tables import (
    CAPTION, DIRECTION, aggregation_cell, bounds, compact, header_unit, inside, lines,
    metrics_in_header, mid, record, row_words, text,
)
from article_analyzer.ruled_pv_tables import NUMERIC_CELL, line_source


SAMPLE_HEADERS = {"sample", "samples", "cells", "device", "devices"}
SAMPLE_TYPE_HEADER = re.compile(r"type\s+of\s+(?:PSCs?|(?:solar\s+)?(?:cells?|devices?))", re.I)
COUNT = re.compile(r"\bbased\s+on\s+(\d+)\s+(?:devices|cells)\b", re.I)
AVERAGE = re.compile(r"\baverag(?:e|es|ed)\b|\bmeans?\b", re.I)
BARE_ID = re.compile(r"[A-Za-z]{1,8}\d+[A-Za-z0-9_-]*")


def metric_slots(head_words):
    headers = sorted(metrics_in_header(head_words), key=lambda h: (h["bbox"][0] + h["bbox"][2]) / 2)
    identities = {(h["metric"], h.get("aggregation", "")) for h in headers}
    if len(headers) < 3 or len(headers) != len(identities):
        return []
    centres = [(h["bbox"][0] + h["bbox"][2]) / 2 for h in headers]
    gaps = [b - a for a, b in zip(centres, centres[1:])]
    if min(gaps) < 18:
        return []
    cuts = [centres[0] - gaps[0] / 2, *((a + b) / 2 for a, b in zip(centres, centres[1:])),
            centres[-1] + gaps[-1] / 2]
    slots = []
    for header, lo, hi in zip(headers, cuts[:-1], cuts[1:], strict=True):
        if not header["unit"]:
            area = [w for w in head_words if lo <= mid(w) < hi]
            unit, parts = header_unit(header["words"][0], area, header["words"], header["metric"], bounded_column=True)
            if unit:
                words = header["words"] + parts
                header = dict(header, unit=unit, words=words, bbox=bounds(words))
        slots.append((lo, hi, header))
    return slots


def row_role(row, metric_start):
    roles = []
    for word in row:
        if word["x1"] >= metric_start:
            continue
        source = {"text": word["text"], "bbox": bounds([word])}
        direction = DIRECTION.fullmatch(word["text"])
        aggregation = aggregation_cell(source)
        if direction or aggregation:
            roles.append((direction[0] if direction else "", aggregation, source))
    return roles[0] if len(roles) == 1 else None


def numeric_values(row, slots):
    cells, undecided = [], []
    for lo, hi, header in slots:
        words = [w for w in row if lo <= mid(w) < hi]
        if not words:
            continue
        source = line_source(words)
        parsed = NUMERIC_CELL.fullmatch(source["text"])
        if parsed and (header["unit"] or header["metric"] == "FF" or "%" in parsed["value"]):
            cells.append((header, words, source, parsed))
        else:
            undecided.append({"metric": header["metric"], **source})
    return cells, undecided


def sample_layout(head_words, role_left):
    headings = [w for w in head_words if w["x1"] < role_left]
    sample = next(([w] for w in headings if compact(w["text"]) in SAMPLE_HEADERS), None)
    type_heading = next((row for row in lines(headings) if SAMPLE_TYPE_HEADER.fullmatch(text(row))), None)
    sample = type_heading or sample
    numbers = next((w for w in headings if compact(w["text"]) in {"number", "numbers", "n"}
                    and any(compact(candidate["text"]) in {"devices", "cells"}
                            and 0 < candidate["top"] - w["top"] < 12
                            and abs(mid(candidate) - mid(w)) < 22 for candidate in headings)), None)
    if sample:
        right = numbers["x0"] - 3 if numbers else role_left - 3
    else:
        if not headings:
            return None
        right = min(w["x0"] for w in headings) - 3
    return right, line_source(sample) if sample else None, numbers


def caption_windows(words):
    rows = lines(words, tolerance=3)
    captions = [(i, CAPTION.match(text(row))) for i, row in enumerate(rows)]
    captions = [(i, match) for i, match in captions if match]
    for (index, match), (stop, _) in zip(captions, captions[1:] + [(len(rows), None)]):
        caption_top = rows[index][0]["top"]
        window = rows[index + 1:stop]
        for body_index, row in enumerate(window):
            first_top = row[0]["top"]
            if first_top - caption_top > 105:
                break
            if sum(NUMERIC_CELL.fullmatch(w["text"]) is not None for w in row) < 3:
                continue
            head_words = [w for line in window[:body_index] for w in line]
            slots = metric_slots(head_words)
            if not slots or len(numeric_values(row, slots)[0]) < 3:
                continue
            header_top = min(header["bbox"][1] for _, _, header in slots)
            caption_words = [w for row in rows[index:index + body_index + 1] for w in row
                             if w["top"] < header_top - 5]
            leaf = [w for w in head_words if w["top"] >= header_top - 5]
            yield match[0].strip(" .|:"), line_source(caption_words), leaf, slots, window[body_index:]
            break


def coordinate_tables(page, number, words):
    """Yield attributed cell candidates and boxes that still need a reader."""
    for table, caption, headings, slots, rows in caption_windows(words):
        max_pce = next((h for _, _, h in slots if h["metric"] == "PCE"
                        and h.get("aggregation") == "champion"), None)
        data_start = slots[0][0]
        first_role = row_role(rows[0], data_start)
        first_left = data_start
        if first_role:
            role_x = first_role[2]["bbox"][0]
            role_lefts = [role[2]["bbox"][0] for row in rows if (role := row_role(row, data_start))
                          and abs(role[2]["bbox"][0] - role_x) <= 12]
            first_left = min(role_lefts)
        layout = sample_layout(headings, first_left - 2)
        if not layout:
            continue
        sample_right, sample_heading, numbers = layout
        if not (sample_heading or first_role):
            continue
        caption_n = COUNT.search(caption["text"])
        average = bool(AVERAGE.search(caption["text"]))
        previous, group, found, review = None, None, [], []
        seen_roles = set()
        for row in rows:
            cells, unresolved = numeric_values(row, slots)
            if len(cells) < 2:
                continue
            paired_mean = max_pce and any(header["metric"] == "PCE"
                                         and header.get("aggregation", "") in {"", "mean"}
                                         and (parsed["spread"] or header.get("aggregation") == "mean")
                                         for header, _, _, parsed in cells)
            y = min(source["bbox"][1] for _, _, source, _ in cells)
            if previous is not None and y - previous > 24:
                break
            bottom = max(source["bbox"][3] for _, _, source, _ in cells) + 2
            role = row_role(row, data_start)
            sample_area = (page.bbox[0], y - 1, sample_right, bottom)
            sample = row_words(page, sample_area)
            if sample and not sample_heading and not BARE_ID.fullmatch(sample):
                sample = ""
            if sample:
                label_words = inside(page.chars, sample_area)
                group = {"text": sample, "bbox": bounds(label_words), "count": None}
                seen_roles = set()
                if numbers:
                    count_words = [w for w in row if sample_right < mid(w) < first_left - 4
                                   and re.fullmatch(r"\d+", w["text"])]
                    if len(count_words) == 1:
                        group["count"] = line_source(count_words)
            direction, aggregation, role_source = role or ("", None, None)
            role_key = compact(role_source["text"]) if role_source else ""
            inherited = bool(not sample and group and role_key and role_key not in seen_roles)
            linked = bool(sample or inherited)
            if not linked:
                group = None
            if role_key:
                seen_roles.add(role_key)
            if not aggregation and direction and average and all(parsed["spread"] for *_, parsed in cells):
                aggregation = dict(caption, value="mean", n_devices="")
            if aggregation and aggregation["value"] == "mean":
                count = group["count"] if group else None
                aggregation = dict(aggregation, n_devices=aggregation["n_devices"] or
                                   (count["text"] if count else caption_n[1] if caption_n else ""))
            for header, cell_words, printed, parsed in cells:
                value = dict(min(cell_words, key=lambda w: w["x0"]), text=parsed["value"].strip())
                entry = record(number, table, header, group["text"] if group else "",
                               group["bbox"] if group else None, value, direction,
                               "continued_sample_group" if inherited else "printed_row",
                               round(y, 3), role_source if direction else None)
                entry["spread_raw"] = parsed["spread"] or ""
                designation = aggregation
                if header.get("aggregation"):
                    designation = dict(entry["sources"]["header"], value=header["aggregation"],
                                       n_devices="", scope="column", basis="printed_statistic")
                elif not designation and paired_mean and parsed["spread"]:
                    # This layout presents means with spreads separately from an explicit maximum.
                    designation = dict(entry["sources"]["header"], value="mean", n_devices="", scope="column",
                                       basis="spread_alongside_maximum",
                                       complementary_header={"text": text(max_pce["words"]), "bbox": max_pce["bbox"]})
                entry["sources"].update(caption=caption, cell=printed, aggregation=designation,
                                        sample_heading=sample_heading, sample_count=group["count"] if group else None)
                if not linked:
                    entry["ambiguity"] = {
                        "reason": "В строке нет однозначной метки образца или продолжения группы.",
                        "alternatives": [],
                    }
                    if role_source:
                        entry["sources"]["unlinked_row_role"] = role_source
                found.append(entry)
            if unresolved:
                review.extend(dict(item, page=number, table=table,
                                   reason="В пределах столбца нет однозначной записи числа и разброса.")
                              for item in unresolved)
            previous = y
        if len({entry["occurrence"] for entry in found}) >= 2:
            extents = [entry["sources"]["cell"]["bbox"] for entry in found]
            box = (min(caption["bbox"][0], *(b[0] for b in extents)), caption["bbox"][1],
                   max(caption["bbox"][2], *(b[2] for b in extents)), max(b[3] for b in extents))
            yield box, found, review

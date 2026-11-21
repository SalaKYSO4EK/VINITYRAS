"""Detect and project printed photovoltaic measurements in ruled PDF tables."""
from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import re
import unicodedata

import pdfplumber

NUMBER = re.compile(r"[+\-−]?\d+(?:[.,]\d+)*(?:\s*%)?")
DIRECTION = re.compile(r"(forward|reverse|FB\s*[–−-]\s*SC|SC\s*[–−-]\s*FB)$", re.I)
CAPTION = re.compile(r"^\s*table\s*(?:s\s*)?\d+[A-Za-z]?\b[. |:]?", re.I)
AGGREGATION_CELL = re.compile(
    r"(?P<champion>best\s*cell|best\s*device|champion(?:\s*(?:cell|device))?)"
    r"|(?P<mean>(?:average|mean)(?:\s*of)?(?:\s*(?P<n>\d+)\s*(?:cells?|devices?))?)"
    r"|(?P<median>median)", re.I,
)


def compact(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text)).casefold()


CONFIG = json.loads((Path(__file__).resolve().parents[1] / "config/solar-pdf-tables.json").read_text(encoding="utf-8"))
METRICS = {compact(name): metric for metric, names in CONFIG["headers"].items() for name in names}
PCE_COLUMN_STATISTICS = CONFIG["pce_column_statistics"]


def unit_key(value):
    return compact(value).replace("−", "-").replace("–", "-").replace("^", "")


def unit_factors(metric):
    metric = unit_family(metric)
    return {unit_key(unit): factor for unit, factor in CONFIG["units"][metric]["factors"].items()}


def unit_family(metric):
    return {"Jsc_EQE": "Jsc", "Eg_PL": "Eg", "Eg_EQE": "Eg"}.get(metric, metric)


def mid(word):
    return (word["x0"] + word["x1"]) / 2


def bounds(words):
    return [round(min(w["x0"] for w in words), 3), round(min(w["top"] for w in words), 3),
            round(max(w["x1"] for w in words), 3), round(max(w["bottom"] for w in words), 3)]


def text(words):
    return " ".join(w["text"] for w in sorted(words, key=lambda w: w["x0"]))


def lines(words, tolerance=2.5):
    groups = []
    for w in sorted(words, key=lambda w: (w["top"], w["x0"])):
        if not groups or w["top"] - groups[-1][0]["top"] > tolerance:
            groups.append([])
        groups[-1].append(w)
    return groups


def inside(words, box):
    x0, top, x1, bottom = box
    return [w for w in words if x0 - 1.5 <= mid(w) <= x1 + 1.5
            and top <= (w["top"] + w["bottom"]) / 2 <= bottom]


def plateaus(page):
    groups = defaultdict(list)
    for edge in page.horizontal_edges:
        if edge["x1"] - edge["x0"] >= 115:
            groups[round(edge["x0"]), round(edge["x1"])].append(edge["top"])
    for (x0, x1), edges in groups.items():
        ruled = []
        for top in sorted(edges):
            if not ruled or top - ruled[-1] > 3.0:
                ruled.append(top)
        for index, top in enumerate(ruled):
            for bottom in ruled[index + 1:index + 4]:
                if 25 <= bottom - top <= 300:
                    yield x0, top, x1, bottom


def metrics_in_header(words, *, bounded_column=False):
    result = []
    for w in words:
        key = compact(w["text"]).split("(", 1)[0]
        marker = METRICS.get(key)
        components = [w]
        aggregation = ""
        if key.startswith("pce"):
            statistic = key[3:].lstrip("_-")
            if key == "pce" and "(" not in w["text"]:
                suffix = next((other for other in words if compact(other["text"]) in PCE_COLUMN_STATISTICS
                               and w["x1"] - .5 <= other["x0"] <= w["x1"] + 8
                               and abs(w["top"] - other["top"]) <= 7), None)
                if suffix:
                    components.append(suffix)
                    statistic = compact(suffix["text"])
            if statistic in PCE_COLUMN_STATISTICS:
                aggregation = PCE_COLUMN_STATISTICS[statistic]
                marker = "PCE"
        if not marker and key in ("v", "j", "fill"):
            next_key = {"v": "oc", "j": "sc", "fill": "factor"}[key]
            auxiliary = next((o for o in words if compact(o["text"]) == next_key
                              and w["x1"] - 0.5 <= o["x0"] <= w["x1"] + 8
                              and abs(w["top"] - o["top"]) <= 7), None)
            if auxiliary:
                marker = METRICS[key + next_key]
                components.append(auxiliary)
        if not marker:
            continue
        if marker == "Jsc":
            suffix = sorted((o for o in words if o not in components
                             and 0 <= o["x0"] - w["x1"] <= 52
                             and abs(o["top"] - w["top"]) <= 3), key=lambda o: o["x0"])
            if len(suffix) >= 2 and [compact(o["text"]) for o in suffix[:2]] in (
                ["from", "ipce"], ["from", "eqe"]
            ):
                marker = "Jsc_EQE"
                components.extend(suffix[:2])
                integrated = [o for o in words if compact(o["text"]) == "integrated"
                              and 0 <= w["x0"] - o["x1"] <= 10
                              and abs(w["top"] - o["top"]) <= 3]
                components.extend(integrated)
        unit, unit_words = header_unit(w, words, components, marker, bounded_column=bounded_column)
        components.extend(unit_words)
        if marker in ("PCE", "Voc") and not unit:
            continue
        header = {"metric": marker, "unit": unit, "words": components, "bbox": bounds(components)}
        if unit_words and unit_words[0]["top"] >= w["bottom"] - 1 and unit_words[0]["x0"] < w["x0"] - 3:
            header["text"] = text([part for part in components if part not in unit_words]) + " " + text(unit_words)
        if aggregation:
            header["aggregation"] = aggregation
        result.append(header)
    return result


def header_unit(anchor, words, components, metric, *, bounded_column=False):
    factors = unit_factors(metric)
    embedded = re.search(r"\((.+)\)$", anchor["text"])
    if embedded and unit_key(embedded[1]) in factors:
        return embedded[1], []
    right = max(word["x1"] for word in components)
    candidates = sorted((o for o in words if o not in components and
                         (bounded_column or (-5 <= o["top"] - anchor["top"] <= 15 and
                                             anchor["x0"] - 3 <= o["x0"] <= right + 60))), key=lambda o: o["x0"])
    matches = []
    for index, first in enumerate(candidates):
        if not first["text"].startswith("(") or (not bounded_column and first["x0"] > right + 13):
            continue
        for count in range(1, min(4, len(candidates) - index) + 1):
            chosen = candidates[index:index + count]
            string = text(chosen)
            if string.endswith(")") and unit_key(string[1:-1]) in factors:
                matches.append((string[1:-1], chosen))
                break
    if len(matches) == 1:
        return matches[0]
    if not matches and not bounded_column:
        for word in words:
            raw = re.fullmatch(r"\((.+)\)", word["text"])
            if (raw and unit_key(raw[1]) in factors and -1 <= word["top"] - anchor["bottom"] <= 9
                    and word["x0"] <= mid(anchor) <= word["x1"] and abs(mid(word) - mid(anchor)) <= 6):
                matches.append((raw[1], [word]))
    return matches[0] if len(matches) == 1 else ("", [])


def bandgap_header(words, y):
    for start in words:
        if abs(start["top"] - y) > 3 or not compact(start["text"]).startswith("e"):
            continue
        nearby = [w for w in words if w is not start and -0.6 <= w["x0"] - start["x1"] <= 10
                  and -1 <= w["top"] - start["top"] <= 4 and not w["text"].startswith("(")]
        label = compact(start["text"] + "".join(w["text"] for w in sorted(nearby, key=lambda w: w["x0"])))
        metric = next((metric for metric, names in CONFIG["bandgap_headers"].items() if label in names), None)
        if metric:
            components = [start, *nearby]
            unit, unit_words = header_unit(start, words, components, metric)
            if unit:
                components.extend(unit_words)
                return {"metric": metric, "unit": unit, "words": components, "bbox": bounds(components)}
    return None


def caption_above(words, box, header_top):
    x0, top, x1, _ = box
    candidates = lines(inside(words, (x0, max(0, top - 52), x1, header_top - 2)), tolerance=5)
    for i in range(len(candidates) - 1, -1, -1):
        value = text(candidates[i])
        if CAPTION.match(value):
            end = min(len(candidates), i + 3)
            # A caption outside the ruling stops at the top edge.
            chosen = [row for row in candidates[i:end]
                      if (row[0]["top"] < top) == (candidates[i][0]["top"] < top)]
            return {"text": "\n".join(text(row) for row in chosen),
                    "bbox": bounds([w for row in chosen for w in row])}
    return {"text": "", "bbox": None}


def table_note(words, box):
    x0, _, x1, bottom = box
    below = lines(inside(words, (x0, bottom + 1, x1, bottom + 38)), tolerance=4)
    if not below or not re.match(r"^(?:notes?\s*[:.]|footnotes?\s*[:.]|[*†‡])", text(below[0]), re.I):
        return None
    note = below[0]
    return {"text": text(note), "bbox": bounds(note)}


def row_words(page, bbox):
    characters = inside(page.chars, bbox)
    output = []
    right = None
    for c in sorted(characters, key=lambda x: (round(x["x0"], 2), x["top"])):
        if right is not None and c["x0"] - right > 1.8:
            output.append(" ")
        output.append(c["text"])
        right = c["x1"]
    return re.sub(r"\s+", " ", "".join(output)).strip()


def number_lines(region, width):
    return [(row, [w for w in row if NUMBER.fullmatch(w["text"])])
            for row in lines(region)
            if len([w for w in row if NUMBER.fullmatch(w["text"])]) >= 3
            # Scan rows can have blank sample and secondary-metric cells.
            and max(w["x1"] for w in row) - min(w["x0"] for w in row) >= width * 0.28]


def record(page_number, table, header, sample, label_box, cell, condition="", label_origin="printed_row",
           occurrence=None, direction_source=None):
    return {"page": page_number, "table": table, "sample": sample, "metric": header["metric"],
            "value_raw": cell["text"], "unit_raw": header["unit"], "condition": condition,
            "label_origin": label_origin, "occurrence": occurrence,
            "unit_origin": "table_header" if header["unit"] else "",
            "sources": {"value": {"bbox": bounds([cell]), "text": cell["text"]},
                        "header": {"bbox": header["bbox"], "text": header.get("text") or text(header["words"])},
                        "sample": {"bbox": label_box, "text": sample},
                        "direction": direction_source}}


def aggregation_cell(source):
    match = AGGREGATION_CELL.fullmatch(source["text"])
    if match:
        value = "mean" if match["mean"] else ("median" if match["median"] else "champion")
        return dict(source, value=value, n_devices=match["n"] or "")
    return None


def longitudinal(page, number, page_words, box, region, body):
    top = min(w["top"] for w in body[0][1])
    head_words = [w for w in region if w["top"] < top - 2]
    headers = metrics_in_header(head_words)
    if len({h["metric"] for h in headers}) < 2 or len(headers) != len({h["metric"] for h in headers}):
        return []
    header_top = min(h["bbox"][1] for h in headers)
    caption = caption_above(page_words, box, header_top)
    match = CAPTION.match(caption["text"])
    if not match:
        return []
    table = match[0].strip(" .|:")
    note = table_note(page_words, box)
    label_end = min(h["bbox"][0] for h in headers) - 2
    label_heading = [w for w in head_words if w["x1"] <= label_end and w["top"] >= header_top - 2]
    label_heading = {"text": text(label_heading), "bbox": bounds(label_heading)} if label_heading else None
    directions_only = bool(re.search(r"\bdirection\b", text(head_words), re.I))
    caption_sample = re.search(r"based on the (.+?)[–-]doped\b", caption["text"].replace("\n", " "), re.I)
    records = []
    last_sample, last_label_box, last_direction, prev_y = "", None, "", None
    for row, numbers in body:
        y = min(w["top"] for w in numbers)
        if prev_y is not None and y - prev_y > 20:
            break
        minimum_height = max(w["bottom"] for w in numbers) - y
        label_box = [box[0], round(y - 2.0, 3), round(label_end, 3), round(y + minimum_height + 3.1, 3)]
        label = row_words(page, label_box)
        direction = DIRECTION.search(label)
        condition = direction[0] if direction else ""
        direction_words = [w for w in row if direction and compact(w["text"]) in compact(condition)
                           and w["x1"] <= label_end + 3]
        direction_source = {"text": condition, "bbox": bounds(direction_words)} if direction_words else None
        label = label[:direction.start()].strip() if direction else label
        kind = "printed_row"
        if not label and directions_only and caption_sample:
            sample = re.sub(r"(?<=[A-Za-z])\s+(?=\d+\b)", "", caption_sample[1]).strip()
            label, label_box, kind = sample, caption["bbox"], "printed_caption"
        elif not label and condition.casefold() == "reverse" and last_direction.casefold() == "forward":
            label, label_box, kind = last_sample, last_label_box, "paired_scan_row"
        if not label:
            prev_y = y
            continue
        statistic_only = aggregation_cell({"text": label, "bbox": label_box})
        for header in headers:
            x0, _, x1, _ = header["bbox"]
            options = [w for w in numbers if x0 - 6 <= mid(w) <= x1 + 6]
            if len(options) == 1:
                row_entry = record(number, table, header, label, label_box, options[0], condition, kind,
                                   round(y, 3), direction_source)
                row_entry["sources"]["caption"] = caption
                row_entry["sources"]["sample_heading"] = label_heading
                row_entry["sources"]["note"] = note
                if statistic_only:
                    row_entry["sample"] = ""
                    row_entry["sources"]["aggregation"] = statistic_only
                    row_entry["ambiguity"] = {"reason": "В строке указана статистика, обозначение образца не установлено.",
                                              "alternatives": []}
                records.append(row_entry)
        last_sample, last_label_box, last_direction, prev_y = ("" if statistic_only else label), label_box, condition, y
    return records


def transverse(number, page_words, box, region, body):
    first = min(w["top"] for w in body[0][1])
    caption = caption_above(page_words, box, first)
    match = CAPTION.match(caption["text"])
    if not match:
        return []
    table = match[0].strip(" .|:")
    note = table_note(page_words, box)
    heads = lines([w for w in region if w["top"] < first - 2])
    records = []
    for row, values in body:
        values = sorted(values, key=mid)
        metric_top = min(w["top"] for w in values)
        left = [w for w in region if w["x1"] < values[0]["x0"] - 3
                and abs(w["top"] - metric_top) <= 7]
        if not left:
            continue
        candidates = metrics_in_header(left)
        if not candidates:
            eg = bandgap_header(left, metric_top)
            if eg:
                candidates.append(eg)
        if len(candidates) != 1:
            continue
        header = candidates[0]
        labels = next((sorted(h, key=mid) for h in reversed(heads)
                       if len(h) == len(values) and all(not NUMBER.fullmatch(w["text"]) for w in h)
                       and all(abs(mid(w) - mid(v)) <= 13 for w, v in zip(sorted(h, key=mid), values))), None)
        if labels is None:
            continue
        for label, value in zip(labels, values, strict=True):
            entry = record(number, table, header, label["text"], bounds([label]), value,
                           label_origin="printed_column", occurrence=round(mid(label), 3))
            entry["sources"]["caption"] = caption
            entry["sources"]["note"] = note
            records.append(entry)
    return records


def detect_pdf(path):
    from article_analyzer.coordinate_pv_tables import coordinate_tables
    from article_analyzer.ruled_pv_tables import merged_tables
    from article_analyzer.visible_pdf import visible_page

    records = {}
    candidates, conflicts, review_regions = [], {}, []
    with pdfplumber.open(path) as pdf:
        for number, original in enumerate(pdf.pages, 1):
            page = visible_page(original)
            words = page.extract_words(x_tolerance=2, y_tolerance=2)
            page_tables = []
            for box in plateaus(page):
                region = inside(words, box)
                body = number_lines(region, box[2] - box[0])
                if not body:
                    continue
                found = longitudinal(page, number, words, box, region, body)
                if not found:
                    found = transverse(number, words, box, region, body)
                if not found:
                    continue
                page_tables.append((box, found))
            covered = {compact(found[0]["table"]) for _, found in page_tables}
            page_tables.extend((box, found) for box, found in merged_tables(page, number)
                               if compact(found[0]["table"]) not in covered)
            for box, found, pending in coordinate_tables(page, number, page.extract_words(x_tolerance=1, y_tolerance=2)):
                table = compact(found[0]["table"])
                prior = [rows for _, rows in page_tables if compact(rows[0]["table"]) == table]
                if prior:
                    old = attributed_cells(r for rows in prior for r in rows)
                    projected = attributed_cells(found)
                    if not old < projected:
                        continue
                    page_tables = [(bounds_, rows) for bounds_, rows in page_tables
                                   if compact(rows[0]["table"]) != table]
                page_tables.append((box, found))
                review_regions.extend(pending)
            for box, found in page_tables:
                candidates.append({"page": number, "bbox": [round(n, 2) for n in box],
                                   "table": found[0]["table"], "records": len(found)})
                for item in found:
                    key = (item["page"], item["metric"], tuple(item["sources"]["value"]["bbox"]))
                    if key not in records:
                        records[key] = item
                    else:
                        first = records[key]
                        signature = ("table", "sample", "condition", "unit_raw")
                        if any(first[k] != item[k] for k in signature):
                            options = conflicts.setdefault(key, [first])
                            if item not in options:
                                options.append(item)
    for key, options in conflicts.items():
        records[key] = dict(records[key], ambiguity={
            "reason": "Пересекающиеся области PDF дают разные связи для одной и той же ячейки.",
            "alternatives": [{k: r[k] for k in ("sample", "condition", "unit_raw", "table", "sources")}
                             for r in options],
        })
    return {"records": sorted(records.values(), key=lambda r: (r["page"], r["sources"]["value"]["bbox"][1],
                                                               r["sources"]["value"]["bbox"][0])),
            "candidate_regions": candidates, "review_regions": review_regions}


def attributed_cells(records):
    """Compare physical numeric cells before replacing a partial ruled result."""
    return {(r["metric"], r["value_raw"], tuple(round(n) for n in r["sources"]["value"]["bbox"]))
            for r in records if r["sample"] and not r.get("ambiguity")}

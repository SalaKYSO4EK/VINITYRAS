"""Recheck overlapping ruled and coordinate interpretations of physical PDF cells."""
import unicodedata

from article_analyzer.photovoltaic_tables import compact, row_words, unit_key
from article_analyzer.ruled_pv_tables import NUMERIC_CELL


def empty_layout_review():
    return {"versions": {"ruled": [], "coordinates": []}, "differences": [],
            "counts": dict.fromkeys(("agreed", "resolved", "needs_review", "single_pass"), 0)}


def cell_key(row):
    # Leading glyph positions are shared even if the other pass includes a spread.
    left, top, _, _ = row["sources"]["value"]["bbox"]
    return row["page"], round(left, 1), round(top, 1)


def label_key(value):
    return " ".join(unicodedata.normalize("NFKC", value).split())


def number_key(value):
    return compact(value).replace("−", "-").replace(",", ".")


def fingerprint(row):
    aggregation = row["sources"].get("aggregation") or {}
    return (compact(row["table"]), row["metric"], label_key(row["sample"]), unit_key(row["unit_raw"]),
            compact(row["condition"]), aggregation.get("value", ""), str(aggregation.get("n_devices") or ""),
            number_key(row["value_raw"]), number_key(row.get("spread_raw", "")))


def index_records(tables, version):
    by_cell = {}
    for region in tables:
        for row in region[1]:
            options = by_cell.setdefault(cell_key(row), {})
            signature = fingerprint(row)
            if signature not in options:
                options[signature] = row
                version.append(row)
    return by_cell


def crop_source(page, source):
    box = source.get("bbox")
    return {"text": row_words(page, box) if box else "", "bbox": box}


def reread_cell(page, candidates):
    boxes = [(row["sources"].get("cell") or row["sources"]["value"])["bbox"] for row in candidates]
    box = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
    return crop_source(page, {"bbox": box})


def complete(row):
    return bool(row["sample"]) and not row.get("ambiguity")


def inspect_difference(page, options, by_pass):
    candidates = list(options.values())
    value_source = reread_cell(page, candidates)
    evidence = {
        "value": value_source,
        "headers": [crop_source(page, row["sources"]["header"]) for row in candidates],
        "samples": [crop_source(page, row["sources"]["sample"]) for row in candidates],
    }
    difference = {"page": candidates[0]["page"], "bbox": value_source["bbox"], "status": "needs_review",
                  "candidates": by_pass, "evidence": evidence,
                  "reason": "Разбор по линиям и координатам даёт разные трактовки ячейки."}
    # Reading the literal box can resolve the printed number or a spread. It cannot
    # by itself settle mismatched attribution to a metric, sample or condition.
    if len({signature[:-2] for signature in options}) != 1:
        return difference, None
    printed = NUMERIC_CELL.fullmatch(value_source["text"])
    if not printed:
        return difference, None
    numeric_signature = number_key(printed["value"]), number_key(printed["spread"] or "")
    matches = [row for signature, row in options.items() if signature[-2:] == numeric_signature]
    if len(matches) == 1 and complete(matches[0]) and all(
        unit_key(header["text"]) == unit_key(row["sources"]["header"]["text"])
        and label_key(sample["text"]) == label_key(row["sample"])
        for row, header, sample in zip(candidates, evidence["headers"], evidence["samples"], strict=True)
    ):
        difference.update(status="resolved", reason="Повторное чтение ячейки подтвердило число и разброс.",
                          confirmed={"value_raw": matches[0]["value_raw"],
                                     "spread_raw": matches[0].get("spread_raw", "")})
        return difference, matches[0]
    return difference, None


def compare_layouts(page, ruled_tables, coordinate_tables, report):
    ruled = index_records(ruled_tables, report["versions"]["ruled"])
    coordinates = index_records(coordinate_tables, report["versions"]["coordinates"])
    counts = report["counts"]
    counts["single_pass"] += len(ruled.keys() ^ coordinates.keys())
    decisions = {}
    for key in sorted(ruled.keys() & coordinates.keys()):
        options = {**ruled[key], **coordinates[key]}
        if len(options) == 1:
            both_complete = all(complete(row) for row in (*ruled[key].values(), *coordinates[key].values()))
            counts["agreed" if both_complete else "single_pass"] += 1
            continue
        # A parser that could read the same number but could not locate its
        # sample or an optional qualifier has supplied a partial interpretation.
        # Keep it in its version, and don't turn a supported row into a conflict.
        if sum(complete(row) for row in options.values()) <= 1 and all(
            len({part for part in field if part}) <= 1 for field in zip(*options, strict=True)
        ):
            counts["single_pass"] += 1
            continue
        by_pass = {"ruled": list(ruled[key].values()), "coordinates": list(coordinates[key].values())}
        difference, confirmed = inspect_difference(page, options, by_pass)
        counts[difference["status"]] += 1
        report["differences"].append(difference)
        decisions[key] = difference, confirmed
    return decisions


def review_selected_cell(row, decisions):
    if row.get("ambiguity") or cell_key(row) not in decisions:
        return row
    difference, confirmed = decisions[cell_key(row)]
    if confirmed is None:
        alternatives = [dict(candidate, layout_pass=pass_name) for pass_name, candidates in
                        difference["candidates"].items() for candidate in candidates]
        return dict(row, ambiguity={"reason": difference["reason"], "alternatives": alternatives,
                                    "source": difference["evidence"]["value"]})
    if fingerprint(row) == fingerprint(confirmed):
        return row
    sources = dict(row["sources"], value=confirmed["sources"]["value"], cell=difference["evidence"]["value"])
    return dict(row, sources=sources, value_raw=confirmed["value_raw"], spread_raw=confirmed.get("spread_raw", ""))

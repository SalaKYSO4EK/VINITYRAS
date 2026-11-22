"""Read aggregation and fabrication context anchored to printed tables."""
from __future__ import annotations

import re

from article_analyzer.jv_methods import cited_span
from article_analyzer.photovoltaic_tables import compact


TABLE_CAPTION = re.compile(r"(?m)^ *(Table[ \t]+S?\d+[a-z]?)[ \t]*[.|:][ \t]*(.*)$", re.I)
REPORTED_SCAN = re.compile(
    r"\bAll\s+J\s*[–−-]\s*V\s+(?:characteristics|curves|measurements)\s+(?:were|are)\s+"
    r"(?:measured|recorded|obtained)\b(?:(?![.!?]\s+[A-Z]).){0,130}?\bwith\s+(?:a\s+)?"
    r"(?P<direction>reverse|forward)\s+scan\b[^.]{0,40}(?:\.\d+[^.]{0,30})?\.\s*"
    r"As\s*shown\s*in\s+(?:(?:Fig(?:ure)?\.?)\s+\d+[a-z]?\s+and\s+)?"
    r"(?P<table>Table\s+S?\d+[a-z]?)\b(?=\s*,)", re.I | re.S)
SMALL_COUNTS = dict(zip(("one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"),
                        map(str, range(1, 11)), strict=True))
GROUP_MEAN = re.compile(r"\b(?P<count>\d+|" + "|".join(SMALL_COUNTS) +
                        r")\s+(?:identical\s+)?(?:devices|cells)\s+were\s+(?:fabricated|prepared|measured)\s+"
                        r"in\s+each\s+group,\s+and\s+the\s+reported\s+values\s+represent\s+"
                        r"the\s+average\s+of\s+the\s+measured\s+data\b\.?", re.I)
HEAT_TREATMENT = re.compile(r"(?P<agent>[a-z0-9][a-z0-9-]*)\s*"
                            r"\((?P<amount>\d+(?:[.,]\d+)?)\s*(?P<unit>[µμu]g\s*/\s*mL)\)\s+"
                            r"(?:substrates|films|samples)\s+were\s+heat[ -]treated\s+at\s+"
                            r"different\s+temperatures\b\.?", re.I)
ANNEALING = re.compile(r"\bannealing\s+temperatures?\b", re.I)


def restore_caption_spacing(cells, pages):
    """Accept a text-layer caption only if the entire geometric caption matches on that page."""
    cache = {}
    for cell in cells:
        caption = cell["sources"].get("caption")
        if not caption or cell["page"] not in pages:
            continue
        key = (cell["page"], caption["text"])
        if key not in cache:
            text = pages[cell["page"]]
            target = compact(caption["text"])
            copies = []
            for start in TABLE_CAPTION.finditer(text):
                if compact(start[1]) != compact(cell["table"]):
                    continue
                stop = start.end()
                for _ in range(12):
                    candidate = compact(text[start.start():stop])
                    if candidate == target:
                        copies.append(cited_span(text, cell["page"], start.start(), stop))
                        break
                    if not target.startswith(candidate) or stop >= len(text):
                        break
                    newline = text.find("\n", stop + 1)
                    stop = newline if newline >= 0 else len(text)
            cache[key] = copies[0] if len(copies) == 1 else None
        original = cache[key]
        if original and " ".join(original["text"].split()) != " ".join(caption["text"].split()):
            cell["sources"]["caption"] = {**caption, **original, "layout_text": caption["text"]}


def table_prose(pages):
    """Bind an immediately adjacent, explicit statement to its page and caption."""
    settings = {}
    scans = {}
    for page, text in pages.items():
        for match in REPORTED_SCAN.finditer(text):
            scans.setdefault(compact(match["table"]), []).append({
                "value": match["direction"].casefold(), "source": cited_span(text, page, match.start(), match.end())})
    for number, text in pages.items():
        captions = list(TABLE_CAPTION.finditer(text))
        for i, caption in enumerate(captions):
            name = caption[1]
            end = min(captions[i + 1].start() if i + 1 < len(captions) else len(text), caption.end() + 1100)
            metadata = {}
            if compact(name) in scans:
                metadata["scan_directions"] = scans[compact(name)]
            mean = GROUP_MEAN.search(text, caption.end(), end)
            if mean:
                count = mean["count"].casefold()
                metadata["aggregation"] = {"value": "mean", "n_devices": SMALL_COUNTS.get(count, count),
                                           "scope": "series", "source": cited_span(text, number, mean.start(), mean.end())}
            if ANNEALING.search(caption[2]):
                start = max(captions[i - 1].end() if i else 0, caption.start() - 950)
                treatment = list(HEAT_TREATMENT.finditer(text, start, caption.start()))
                if len(treatment) == 1 and re.search(r"\b" + re.escape(name) + r"\b", text[treatment[0].end():caption.start()], re.I):
                    spec = treatment[0]
                    previous_sentence = text.rfind(". ", start, spec.start())
                    sentence_start = (previous_sentence + 2 if previous_sentence >= 0
                                      and spec.start() - previous_sentence <= 150 else spec.start())
                    metadata["fabrication"] = {
                        "agent": spec["agent"], "concentration": spec["amount"].replace(",", "."), "unit": "µg/mL",
                        "source": cited_span(text, number, sentence_start, spec.end()),
                    }
            if metadata:
                settings[number, compact(name)] = metadata
    return settings


def apply_table_prose(row, settings):
    metadata = settings.get((row["page"], compact(row["table"])), {})
    context = row["context"]
    options = metadata.get("scan_directions", [])
    if options and not context["scan_direction_raw"] and not context["scan_direction"]:
        if len({option["value"] for option in options}) == 1:
            context["scan_direction"] = options[0]["value"]
            context["scan_direction_source"] = options[0]["source"]
        else:
            context["ambiguities"]["scan_direction"] = {
                "reason": "Текст связывает таблицу с несколькими направлениями сканирования.", "alternatives": options}
    if metadata.get("aggregation") and "aggregation" not in context and "aggregation" not in context["ambiguities"]:
        context["aggregation"] = metadata["aggregation"]
    heading = row["sources"].get("sample_heading") or {}
    heading_text = compact(heading.get("text", "")).replace("◦", "°").replace("º", "°")
    if (metadata.get("fabrication") and heading_text == "temperature(°c)"
            and re.fullmatch(r"[+\-]?\d+(?:[.,]\d+)?", row["sample"])):
        context["fabrication"] = dict(metadata["fabrication"], annealing_temperature={
            "value": row["sample"], "unit": "°C",
            "source": {"page": row["page"], "cell": row["sources"]["sample"], "header": heading},
        })

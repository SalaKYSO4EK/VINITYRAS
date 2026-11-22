"""Read RAG evaluation cells whose printed row and column are geometrically anchored."""
from __future__ import annotations

from dataclasses import dataclass
import re
from statistics import median

from .photovoltaic_tables import bounds, lines, mid, text
from .visible_pdf import visible_page


METRIC = re.compile(
    r"(?:EM|Exact\s+Match|F[- ]?1|Acc(?:uracy)?\.?|Precision|Recall(?:@\d+)?|"
    r"(?:P|R|Hits?)@\d+|(?:MRR|NDCG|MAP|ROUGE(?:-[L12])?|BLEU)(?:@\d+)?|"
    r"Correct|Incorrect|Missing|Factuality|Latency|Time|Steps?)", re.I,
)
METHOD = re.compile(r"(?:Methods?|Systems?|Approach(?:es)?|Configurations?|Models?|Baselines?)", re.I)
ROLE = re.compile(r"(?:Methods?|Systems?|Approach(?:es)?|Configurations?|Models?|Baselines?|"
                  r"Types?|Data|Datasets?|Benchmarks?|Generators?|LLMs?)", re.I)
DATASET = re.compile(r"(?:Hotpot(?:QA)?|2Wiki(?:MultiHopQA)?|MuSiQue|SQuAD(?:\s*v?\d[.\d]*)?|"
                     r"Natural\s+Questions|NQ|TriviaQA|WebQuestions|WebQ|PopQA|ASQA|"
                     r"CRAG|KILT|BEIR|TREC(?:-DL)?|MS\s*MARCO|NFCorpus|TAT-QA|FinQA|"
                     r"TechQA|PubMedQA|CovidQA|HAGRID|ExpertQA|DelucionQA|Emanual)", re.I)
MODEL = re.compile(r"(?:GPT[-\s]?\d|LLaMA[-\s]?\d|Qwen|FLAN[-\s]|T5[-\s]|Mistral|Mixtral|"
                   r"Claude|Gemini|DeepSeek|OPT-)", re.I)
NUMBER = r"[+\-−]?(?:\d+(?:[.,]\d+)?|[.,]\d+)(?:[eE][+\-−]?\d+)?"
UNIT = r"%|ms|s|sec(?:onds?)?|minutes?|tokens?|fraction|p\.p\."
NUMERIC = re.compile(rf"{NUMBER}(?:\s*(?:±|\+/-)\s*{NUMBER})?(?:\s*(?P<unit>{UNIT}))?", re.I)
CAPTION = re.compile(r"^Table\s+((?:[A-Z]?\d+[a-z]?|[IVXLCDM]+))\s*(?:[:.]|$)", re.I)
GLOBAL_DATASET = re.compile(rf"\b(?:using|on)\s+(?:the\s+)?({DATASET.pattern})\s+dataset\b", re.I)
GLOBAL_MODEL = re.compile(r"\b(?:with|using)\s+([\w.‐‑–\-]+(?:\s+\([\w.\-]+\))?)\s+as\s+the\s+LLM\b", re.I)
SHARED_METRIC = re.compile(r"\b(factuality|accuracy|precision|recall|exact\s+match|F1)\s+scores?\b", re.I)


@dataclass(frozen=True)
class Slot:
    left: float
    right: float
    heading: list
    metric: str
    unit: str = ""


def yw(word):
    return (word["top"] + word["bottom"]) / 2


def line_top(words):
    return min(word["top"] for word in words)


def numeric_slots(row):
    words = sorted(row, key=lambda word: word["x0"])
    headings = []
    while words:
        rest = words[1:]
        for width in (2, 1):
            part = words[:width]
            if len(part) != width or any(right["x0"] - left["x1"] > 5 for left, right in zip(part, part[1:])):
                continue
            raw = text(part)
            match = re.fullmatch(rf"(?P<metric>.*?)(?:\s*\((?P<unit>{UNIT})\)|\s*\[(?P<other_unit>{UNIT})\])?",
                                 raw, re.I)
            if match and METRIC.fullmatch(match["metric"]):
                headings.append((part, match["metric"], match["unit"] or match["other_unit"] or ""))
                rest = words[width:]
                break
        words = rest
    return aligned_slots(headings)


def aligned_slots(headings, centres=None):
    if len(headings) < 2:
        return []
    centres = centres or [(part[0]["x0"] + part[-1]["x1"]) / 2 for part, _, _ in headings]
    gaps = [right - left for left, right in zip(centres, centres[1:])]
    if min(gaps) < 9 or max(gaps) > max(70, 3 * median(gaps)):
        return []
    cuts = [centres[0] - gaps[0] / 2, *((left + right) / 2 for left, right in zip(centres, centres[1:])),
            centres[-1] + gaps[-1] / 2]
    return [Slot(cuts[index], cuts[index + 1], part, metric, unit)
            for index, (part, metric, unit) in enumerate(headings)]


def caption_lines(rows):
    captions = []
    for index, row in enumerate(rows):
        words = sorted(row, key=lambda word: word["x0"])
        for offset, first in enumerate(words[:-1]):
            if first["text"].casefold() != "table":
                continue
            if offset and first["x0"] - words[offset - 1]["x1"] < 18:
                continue
            prefix = text(words[offset:offset + 2])
            match = CAPTION.match(prefix)
            if not match or words[offset + 1]["x0"] - first["x1"] > 10:
                continue
            same_line = [first]
            for word in words[offset + 1:]:
                if word["x0"] - same_line[-1]["x1"] > 27:
                    break
                same_line.append(word)
            parts = list(same_line)
            for following in rows[index + 1:index + 5]:
                previous = max(word["top"] for word in parts)
                preceding = [word for word in parts if previous - word["top"] < 2.5]
                if text(preceding).endswith(".") or line_top(following) - previous > 14:
                    break
                local = [word for word in following if first["x0"] - 8 <= mid(word) <= same_line[-1]["x1"] + 5]
                if (len(local) < 4 or any(NUMERIC.fullmatch(w["text"]) or METRIC.fullmatch(w["text"])
                                          for w in local)):
                    break
                parts.extend(local)
            captions.append((f"Table {match[1]}", first["top"], first["x0"], parts))
    return captions


def find_label_heading(rows, left, expression):
    words = sorted((word for row in rows for word in row
                    if word["x1"] <= left + 3 and ROLE.fullmatch(word["text"])), key=mid)
    options = sorted((word for word in words if expression.fullmatch(word["text"])), key=mid)
    if not options:
        return None
    heading = options[-1]
    previous = [word for word in words if mid(word) < mid(heading) - 10]
    return heading, (mid(previous[-1]) + mid(heading)) / 2 if previous else float("-inf")


def partition_names(slots, head_rows):
    names = {"generator": {}, "dataset": {}}
    measures = tuple(re.sub(r"\W", "", slot.metric.casefold()) for slot in slots)
    widths = [width for width in range(2, len(slots) + 1)
              if len(slots) % width == 0 and len(set(measures[:width])) > 1
              and measures[:width] * (len(slots) // width) == measures]
    for row in reversed(head_rows):
        for width in widths:
            chunks = [slots[offset:offset + width] for offset in range(0, len(slots), width)]
            spans = [[word for word in row if chunk[0].left <= mid(word) < chunk[-1].right] for chunk in chunks]
            candidates = [text(words) for words in spans]
            for kind, matches in (("dataset", DATASET.fullmatch),
                                  ("generator", lambda value: MODEL.match(value)
                                   and not any(MODEL.match(word) for word in value.split()[1:]))):
                if not names[kind] and all(matches(value) for value in candidates):
                    names[kind] = {index: spans[index // width] for index in range(len(slots))}
    return names


def scored_line(row, slots):
    values = {}
    for index, slot in enumerate(slots):
        part = [word for word in row if slot.left <= mid(word) < slot.right]
        raw = text(part)
        parsed = NUMERIC.fullmatch(raw)
        if parsed:
            values[index] = (part, raw, parsed["unit"] or slot.unit)
    return values


def scored_body(rows, start, stop, slots):
    seen = []
    for row in rows[start:stop]:
        values = scored_line(row, slots)
        top = line_top(row)
        if seen and top - seen[-1][0] > 32:
            break
        if not seen and top - line_top(rows[start - 1]) > 38:
            break
        if len(values) >= max(2, len(slots) // 2):
            seen.append((top, values))
    return seen


def choose_caption(captions, left, head_top, last_top):
    compatible = [item for item in captions if item[2] <= left + 31
                  and (head_top - 165 <= item[1] <= head_top or last_top <= item[1] <= last_top + 48)]
    if not compatible:
        return None
    scored = sorted(((min(abs(top - head_top), abs(top - last_top)),
                      table, top, x, words) for table, top, x, words in compatible),
                    key=lambda item: item[0])
    if len(scored) > 1 and scored[1][0] - scored[0][0] < 4:
        return None
    return scored[0][1:]


def row_labels(words, body, index, left, right):
    top, values = body[index]
    centre = median(yw(part[0]) for part, _, _ in values.values())
    prior_gap = top - body[index - 1][0] if index else 11
    next_gap = body[index + 1][0] - top if index + 1 < len(body) else 11
    return [word for word in words if left <= mid(word) < right
            and centre - min(6.5, prior_gap / 2) <= yw(word) < centre + min(6.5, next_gap / 2)]


def by_dataset_rows(words, rows, captions, page_number):
    """A shared caption metric, one method per column, printed dataset in the row."""
    records = []
    for index, row in enumerate(rows[:-2]):
        ordered = sorted(row, key=lambda word: word["x0"])
        first = ordered[0]
        if not re.fullmatch(r"Datasets?|Benchmarks?", first["text"], re.I):
            continue
        if len(ordered) < 3:
            continue
        slots = []
        for following in rows[index + 1:index + 5]:
            centres = sorted(mid(word) for word in following
                             if word["x0"] > first["x1"] + 12 and NUMERIC.fullmatch(word["text"]))
            if len(centres) < 2 or len(centres) > len(ordered) - 1:
                continue
            windows = aligned_slots([([], "", "")] * len(centres), centres)
            possible = [[word for word in ordered[1:] if window.left <= mid(word) < window.right]
                        for window in windows]
            if windows and all(possible) and sum(map(len, possible)) == len(ordered) - 1:
                slots = aligned_slots([(heading, "", "") for heading in possible], centres)
                break
        if not slots:
            continue
        body = scored_body(rows, index + 1, len(rows), slots)
        if len(body) < 2:
            continue
        caption = choose_caption(captions, first["x0"], line_top(row), body[-1][0])
        if not caption:
            continue
        table, _, _, caption_words = caption
        caption_text = " ".join(text(line) for line in lines(caption_words))
        metrics = SHARED_METRIC.findall(caption_text)
        if len(set(metric.casefold() for metric in metrics)) != 1:
            continue
        for row_index, (_, values) in enumerate(body):
            label = row_labels(words, body, row_index, first["x0"] - 5, slots[0].left)
            recorded_label = text(label)
            dataset = recorded_label.partition("(")[0].strip()
            if not any(char.isalpha() for char in dataset):
                continue
            average = re.fullmatch(r"Avg\.?|Average", dataset, re.I)
            for column, (cell, raw, unit) in values.items():
                heading = slots[column].heading
                records.append({
                    "page": page_number, "table": table, "sample": text(heading),
                    "generator": "", "dataset": "" if average else dataset, "metric": metrics[0], "raw": raw, "unit": unit,
                    "aggregation": "average" if average else "",
                    "fragment": raw, "bbox": bounds(cell), "row_fragment": recorded_label, "row_bbox": bounds(label),
                    "header_fragment": text(heading), "header_bbox": bounds(heading), "caption_fragment": caption_text,
                })
    return records


def extract_table_cells(page, page_number):
    """Return raw numbers and physical PDF evidence. Unresolved cells are omitted."""
    visible = visible_page(page)
    try:
        words = visible.extract_words(x_tolerance=1.1, y_tolerance=2)
    finally:
        if visible is not page:
            visible.close()
    rows = lines(words, tolerance=2.4)
    candidates = [(index, slots) for index, row in enumerate(rows) if (slots := numeric_slots(row))]
    captions = caption_lines(rows)
    records = []
    layout = candidates + [(len(rows), [])]
    for (index, slots), (stop, _) in zip(layout, layout[1:]):
        head_top = line_top(rows[index])
        nearby = [row for row in rows[max(0, index - 8):index + 1] if head_top - line_top(row) <= 40]
        label_headings = [row for row in nearby if head_top - line_top(row) <= 20]
        method = find_label_heading(label_headings, slots[0].left, METHOD)
        if not method:
            continue
        method_word, left = method
        body = scored_body(rows, index + 1, stop, slots)
        if len(body) < 2:
            continue
        caption = choose_caption(captions, method_word["x0"], head_top, body[-1][0])
        if not caption:
            continue
        table, _, _, caption_words = caption
        caption_text = " ".join(text(row) for row in lines(caption_words))
        global_data = GLOBAL_DATASET.search(caption_text)
        global_model = GLOBAL_MODEL.search(caption_text)
        groups = partition_names(slots, nearby[:-1])
        for row_index, (_, values) in enumerate(body):
            label = row_labels(words, body, row_index, left, slots[0].left)
            sample = text(label)
            if not sample or not any(char.isalpha() for char in sample):
                continue
            for column, (cell, raw, unit) in values.items():
                slot = slots[column]
                dataset = groups["dataset"].get(column, [])
                generator = groups["generator"].get(column, [])
                header = [*generator, *dataset, *slot.heading]
                model = text(generator) or (global_model[1] if global_model else "")
                uncertain_model = bool(model and re.search(r"[*∗†‡]", sample))
                records.append({
                    "page": page_number, "table": table, "sample": sample,
                    "generator": "" if uncertain_model else model,
                    "dataset": text(dataset) or (global_data[1] if global_data else ""),
                    "metric": slot.metric, "raw": raw, "unit": unit, "fragment": raw, "bbox": bounds(cell),
                    "row_fragment": sample, "row_bbox": bounds(label),
                    "header_fragment": " ".join(text(row) for row in lines(header)),
                    "header_bbox": bounds(header), "caption_fragment": caption_text,
                    **({"generator_heading": model,
                        "generator_note": "Method has a printed footnote marker; row-specific generator requires verification."}
                       if uncertain_model else {}),
                })
    occupied = {tuple(record["bbox"]) for record in records}
    records.extend(record for record in by_dataset_rows(words, rows, captions, page_number)
                   if tuple(record["bbox"]) not in occupied)
    return records

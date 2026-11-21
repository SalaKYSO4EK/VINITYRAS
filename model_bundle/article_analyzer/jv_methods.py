"""Link explicitly reported J–V illumination to matching performance tables."""
from __future__ import annotations

from decimal import Decimal
import re

from pypdf import PdfReader

from article_analyzer.photovoltaic_tables import compact
from article_analyzer.pv_checks import amount, illumination_options


JV_METHOD = re.compile(r"\bJ\s*[–−-]?\s*V\b.{0,125}\b(?:measur(?:ed|ements?)|recorded|performed|obtained)\b",
                       re.I | re.S)
METHOD_SECTION = re.compile(r"\b(?:(?:device|pv|solar cell)\s+characteri[sz]ation|"
                            r"materials and methods|experimental section|measurement methods)\b", re.I)
GENERAL_METHOD = re.compile(r"\b(?:under|at)\b", re.I)
JV_CURVES = re.compile(r"\bJ\s*[–−-]?\s*V\b", re.I)
TABLE_LINK = re.compile(r"\bcorresponding\s+(?:parameters|results|values)(?:\s+for\s+[^.]{1,190}?)?\s+"
                        r"(?:are|were)\s+(?:summari[sz]ed|listed|reported|presented|given|tabulated|detailed)\s+in\s+"
                        r"(Table\s+S?\d+[A-Za-z]?)\b", re.I)
FIGURE_JV = re.compile(r"\b(?:Fig(?:ure)?\.?)\s+(?P<number>\d+)(?P<panel>[a-z])\s+"
                       r"(?:illustrates?|shows?|presents?)\s+(?:the\s+)?J\s*[–−-]?\s*V\s+"
                       r"(?:characteristics|curves)\b", re.I)
PANEL = re.compile(r"\(([a-z])\)(?=\s)", re.I)
RESTRICTED = re.compile(r"\b(?:specific|only|exclusively)\b", re.I)
SCAN_PROTOCOL = re.compile(r"\b(?:sweeping|scanning)\s+con(?:-\s*)?ditions?\s+(?:are|were|include[sd]?)\b", re.I)
SCAN_DIRECTION = re.compile(r"\b(reverse|forward)\s*(?:scan|sweep)\b", re.I)
TABLE_CONTEXT = re.compile(r"\b(?:photovoltaic|pv|(?:device|cell)\s+performance)\b", re.I)
EXTERNAL = re.compile(r"\b(?:literature|previous studies|previously reported|published devices|"
                      r"certified|certification|stabili[sz]ed|outdoor|indoors?|"
                      r"low[- ]light|light intensities|simulated parameters|dark\s+J\s*[–−-]?\s*V)\b", re.I)


def cited_span(text, page, start, end):
    excerpt = text[start:end].rstrip()
    return {"page": page, "text": excerpt, "start": start, "end": start + len(excerpt)}


def adjacent_scan_protocol(text, page, after):
    # A description in the same methods paragraph may expressly cover both directions.
    following = text[after:after + 800]
    boundary = re.search(r"\n\s*(?:Table|Figure)\s*\d+\b|\n\s*[\w ]{3,48}:", following, re.I)
    if boundary:
        following = following[:boundary.start()]
    start = SCAN_PROTOCOL.search(following)
    if not start:
        return None
    paragraph = following[start.start():]
    ending = re.search(r"(?<=[.!?])\s+(?=[A-Z])", paragraph)
    stop = start.start() + (ending.start() if ending else len(paragraph))
    source = cited_span(text, page, after + start.start(), after + stop)
    directions = sorted({match[1].casefold() for match in SCAN_DIRECTION.finditer(source["text"])})
    return dict(source, directions=directions) if directions else None


def article_page_texts(path):
    return {number: page.extract_text() or "" for number, page in enumerate(PdfReader(path).pages, 1)}


def figure_illumination(pages):
    """Follow an explicit Figure-panel-to-table reference, including across one page break."""
    for page, text in pages.items():
        for link in TABLE_LINK.finditer(text):
            # The inline Figure reference must immediately precede the table link.
            start = max(0, link.start() - 650)
            prior = pages.get(page - 1, "") if start == 0 else ""
            figure_text = prior[-650:] + text[start:link.start()]
            references = list(FIGURE_JV.finditer(figure_text))
            if not references:
                continue
            figure = references[-1]
            caption = re.search(r"(?m)^ *Figure\s+" + figure["number"] + r"\.\s*", text, re.I)
            if not caption or caption.start() < link.end():
                continue
            boundary = re.search(r"(?m)^\s*(?:Table|Figure)\s+\d+\.", text[caption.end():])
            tail = text[caption.end():caption.end() + (boundary.start() if boundary else 1000)]
            panels = list(PANEL.finditer(tail))
            for index, panel in enumerate(panels):
                if panel[1].casefold() != figure["panel"].casefold():
                    continue
                stop = panels[index + 1].start() if index + 1 < len(panels) else len(tail)
                source = cited_span(text, page, caption.end() + panel.start(), caption.end() + stop)
                if not JV_CURVES.search(source["text"]) or EXTERNAL.search(source["text"]):
                    continue
                options = {}
                for name, value in illumination_options(source, "methods"):
                    options.setdefault(name, []).append(value)
                if "pin" not in options or "spectrum" not in options:
                    continue
                offset = len(prior[-650:])
                if figure.start() < offset:
                    discussion = cited_span(prior, page - 1, len(prior) - offset + figure.start(), len(prior))
                else:
                    discussion = cited_span(text, page, start + figure.start() - offset, link.start())
                yield {"table": link[1], "options": options, "directions": None, "scan_protocol": None,
                       "figure": {"label": f"Figure {figure['number']}{figure['panel']}",
                                  "aggregation": "champion" if re.search(r"\bchampion\b", discussion["text"], re.I) else "",
                                  "discussion": discussion,
                                  "table_reference": cited_span(text, page, link.start(), link.end())}}


def illumination_methods(path, *, pages=None):
    """Keep the exact sentence, page and text offsets used for each candidate."""
    candidates = []
    pages = article_page_texts(path) if pages is None else pages
    for number, text in pages.items():
        method_section = METHOD_SECTION.search(text)
        if not method_section and not TABLE_LINK.search(text):
            continue
        starts = [0, *(match.end() for match in re.finditer(r"(?<=[.!?])\s+(?=[A-Z])", text)), len(text)]
        for start, stop in zip(starts, starts[1:]):
            sentence = text[start:stop].rstrip()
            table = TABLE_LINK.search(sentence)
            linked = table and JV_CURVES.search(sentence) and GENERAL_METHOD.search(sentence)
            restriction = None
            if not linked:
                if not method_section:
                    continue
                method_match = JV_METHOD.search(sentence)
                if not method_match or not GENERAL_METHOD.search(sentence[method_match.end():]):
                    continue
                nearby = text[max(0, start - 2400):start + method_match.start()]
                if not METHOD_SECTION.search(nearby):
                    continue
                directions = {match[1].casefold() for match in SCAN_DIRECTION.finditer(sentence)}
                if directions or RESTRICTED.search(sentence):
                    restriction = sorted(directions) if len(directions) == 1 else []
            source = cited_span(text, number, start, stop)
            parsed = {}
            for name, option in illumination_options(source, "methods"):
                parsed.setdefault(name, []).append(option)
            if "pin" in parsed or "spectrum" in parsed:
                protocol = adjacent_scan_protocol(text, number, source["end"]) if not linked and restriction is None else None
                # A list of two scan directions is affirmative evidence for the common method.
                if protocol:
                    restriction = protocol["directions"]
                candidate = {"options": parsed, "directions": restriction, "scan_protocol": protocol}
                if linked:
                    candidate["table"] = table[1]
                candidates.append(candidate)
    return [*candidates, *figure_illumination(pages)]


def is_performance_group(group):
    metrics = {row["metric"] for row in group if not row.get("ambiguity")}
    if not {"Voc", "Jsc", "FF", "PCE"}.issubset(metrics):
        return False
    efficiency = next((row for row in group if row["metric"] == "PCE"), None)
    caption = group[0]["sources"].get("caption")
    if not efficiency or not caption:
        return False
    text = " ".join(caption["text"].split())
    return bool(TABLE_CONTEXT.search(text) and not EXTERNAL.search(text)
                and amount(efficiency["value_raw"]) > 0)


def option_key(name, option):
    return Decimal(option["value"]) if name == "pin" else option["value"].lower().replace("-", " ")


def apply_illumination_methods(context, candidates, basis=None):
    """Use a unique compatible protocol. Leave unresolved conflicts visible."""
    table = compact(basis.get("table", "")) if basis else ""
    specific = [candidate for candidate in candidates if table and compact(candidate.get("table", "")) == table]
    candidates = specific or [candidate for candidate in candidates if not candidate.get("table")]
    compatible, unresolved = [], []
    for candidate in candidates:
        direction = context["scan_direction"]
        directions = candidate["directions"]
        values = candidate["options"]
        if any(name in context and all(option_key(name, value) != option_key(name, context[name])
                                       for value in values[name])
               for name in ("pin", "spectrum") if name in values):
            continue
        if directions is not None and direction not in directions:
            if not directions or not direction:
                unresolved.append(values)
            continue
        compatible.append(candidate)

    applied = {}
    for name in ("pin", "spectrum", "insolation"):
        if name in context or name in context["ambiguities"] or "note_scope" in context["ambiguities"]:
            continue
        by_value, alternatives = {}, {}
        for candidate in compatible:
            for option in candidate["options"].get(name, ()):
                by_value.setdefault(option_key(name, option), (option, candidate))
        for values in unresolved:
            for option in values.get(name, ()):
                alternatives.setdefault(option_key(name, option), option)
        missing = any(name not in candidate["options"] for candidate in compatible)
        if len(by_value) == 1 and not missing and set(alternatives) <= by_value.keys():
            option, candidate = next(iter(by_value.values()))
            context[name] = option
            applied[name] = candidate
        elif alternatives or by_value:
            alternatives.update((key, option) for key, (option, _) in by_value.items())
            context["ambiguities"][name] = {
                "reason": "Применимость светового режима к этой строке требует сверки.",
                "alternatives": list(alternatives.values()),
            }
    if basis and (applied or any(key in context["ambiguities"] for key in ("pin", "spectrum", "insolation"))):
        reference = applied.get("pin") or applied.get("spectrum") or applied.get("insolation")
        protocol = reference.get("scan_protocol") if reference else None
        pending = any(key in context["ambiguities"] for key in ("pin", "spectrum", "insolation"))
        reason = ("Совместимые условия и направление подтверждены в методике." if protocol else
                  "Сопоставлены тип измерения, подпись таблицы и световой режим из методики.")
        if reference and reference.get("table"):
            reason = "В тексте прямо указано, что таблица сводит параметры этих кривых ВАХ."
        if reference and reference.get("figure"):
            basis = dict(basis, figure=reference["figure"])
            reason = "В тексте указана связь таблицы с рисунком, световой режим приведён в подписи панели."
        context["illumination_link"] = dict(
            basis, status="needs_review" if pending else "confirmed",
            scan_direction=context["scan_direction"],
            covered_directions=reference["directions"] if reference is not None else None,
            scan_protocol=protocol, fields=list(applied),
            reason=reason,
        )

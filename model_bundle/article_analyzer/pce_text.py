"""Recognize explicitly reported photovoltaic efficiency in visible PDF prose."""
import re
from decimal import Decimal

from .evidence import source_fragments


_METRIC = (
    r"\b(?:PCEs?|power\s+conversion\s+efficienc(?:y|ies)"
    r"|КПД|эффективност(?:ь|и)\s+преобразования(?:\s+энергии)?)\b(?:\s*\(\s*PCEs?\s*\))?"
)
_PERCENT = r"(?<![\w+−–-])(?<!\d[.,])(?P<number>\d{1,3}(?:[.,]\d+)?)\s*(?P<unit>[%％])"
_VERB = r"was|is|reached|attained|equal(?:s|led)|составил(?:а|о)?|достиг(?:ла|ло)?"
_METRIC_NAME = re.compile(_METRIC, re.I)
_STATED = re.compile(
    rf"(?:{_METRIC}(?:\s*(?:=|:)|\s+(?:of|{_VERB})\s+"
    rf"|\s+(?:of|for)\s+[^,;:.%\n]{{1,60}}?\s+(?:{_VERB})\s+)\s*{_PERCENT})",
    re.IGNORECASE,
)
_PREFIXED = re.compile(rf"{_PERCENT}\s+(?:certified\s+)?{_METRIC}", re.IGNORECASE)
_NON_RESULT = re.compile(
    r"\[\s*\d+(?:\s*[,;–—-]\s*\d+)*\s*\]|\bet\s+al\b|\b(?:previous(?:ly)?|literature|"
    r"theoretical(?:ly)?|predicted|simulated|estimated|expected|according\s+to|reported\s+by|"
    r"retention|retained|relative|up\s+to|less\s+than|more\s+than|higher\s+than|lower\s+than|"
    r"gain|loss|boost|improvement|degradation|target|not|unmeasured|respectively)\b"
    r"|\b(?:ранее|теоретическ\w*|оценочн\w*|моделирован\w*|прогноз\w*|не|прирост|"
    r"повышение|снижение|соответственно)\b|\d\s*[%％]?\s*[−–—-]\s*\d|[%％]\s*(?:\(\d+\)|\d)",
    re.IGNORECASE,
)
_REFERENCES = re.compile(r"^\s*(?:references|bibliography|литература|список\s+литературы)\s*$", re.I | re.M)
_AGGREGATIONS = {
    "mean": re.compile(r"\b(?:mean|average|averaged|средн\w*)\b", re.I),
    "champion": re.compile(r"\b(?:champion|maximum|best|highest|максимальн\w*|лучш\w*)\b", re.I),
}


def text_pce(pages: dict[int, str]) -> list[dict]:
    """Return explicit values and their literal visible source spans."""
    found = {}
    for page, text in pages.items():
        reference = _REFERENCES.search(text)
        if reference:
            text = text[:reference.start()]
        if _METRIC_NAME.search(text):
            for fragment in source_fragments({page: text}, max_chars=max(1, len(text))).values():
                value = _source_value(fragment.text)
                if value:
                    value["source_page"] = page
                    value["source_fragment"] = fragment.text
                    key = (Decimal(value["metric_value"]), value["aggregation"])
                    found.setdefault(key, value)
        if reference:
            break
    return list(found.values())


def _source_value(text: str) -> dict | None:
    if len(text) > 600 or text.count("%") + text.count("％") != 1 or _NON_RESULT.search(text):
        return None
    match = _STATED.search(text) or _PREFIXED.search(text)
    if not match:
        return None
    raw = match["number"]
    number = raw.replace(",", ".")
    if not 0 <= Decimal(number) <= 100:
        return None
    aggregations = [name for name, marker in _AGGREGATIONS.items() if marker.search(text)]
    if len(aggregations) > 1:
        return None
    return {
        "metric_name": "PCE",
        "metric_value": number,
        "metric_unit": "%",
        "raw_value": raw,
        "raw_unit": match["unit"],
        "aggregation": aggregations[0] if aggregations else "",
    }


def find_pce_in_text(path) -> list[dict]:
    """Read visible text in physical page order, including inside clipped PDF forms."""
    import pdfplumber

    from .visible_pdf import visible_page

    texts = {}
    with pdfplumber.open(path) as pdf:
        for raw_page in pdf.pages:
            page = visible_page(raw_page)
            try:
                text = page.extract_text(use_text_flow=True) or ""
                if text.strip():
                    texts[page.page_number] = text
                if _REFERENCES.search(text):
                    break
            finally:
                page.close()
                raw_page.close()
    if not texts:
        raise ValueError("В PDF нет доступного для извлечения текста.")
    return text_pce(texts)

"""Collect source-backed candidates for short PCE and Voc requests."""
from decimal import Decimal
import re
import unicodedata

from pypdf import PdfReader


METRIC = r"(?:PCE|V_?oc|КПД)"
REQUEST = re.compile(
    rf"^(?:извлеки(?:те)?|найди(?:те)?|укажи(?:те)?|extract)\s+(?:все\s+)?(?:значения\s+)?"
    rf"(?P<metrics>{METRIC}(?:\s*(?:,|и|and)\s*{METRIC})*)"
    rf"(?:\s*(?:(?:,?\s*и\s+)?укажи(?:те)?\s+источники(?:\s+в\s+PDF)?|with\s+sources))?[.!]?$",
    re.I,
)
DECIMAL = r"(?P<number>\d{1,3}(?:[.,]\d+)?)"
PCE = r"(?:PCE|(?:power\s+)?conversion\s+efficiency|efficiency|Eff)"
VOC = r"(?:V\s*_?\s*oc|open[\s-]*circuit[\s-]+(?:photo)?voltage(?:\s*\(\s*V\s*oc\s*\))?)"
PCE_AFTER = re.compile(rf"\b{PCE}\b[^.;,:\d%]{{0,34}}?{DECIMAL}\s*(?P<unit>%)", re.I)
PCE_BEFORE = re.compile(rf"{DECIMAL}\s*(?P<unit>%)\s+(?:certified\s+)?(?:PCE|(?:power\s+)?conversion\s+efficiency)\b", re.I)
VOC_AFTER = re.compile(rf"\b{VOC}\b[^.;,:\d%]{{0,34}}?{DECIMAL}\s*(?P<unit>mV|V)\b", re.I)
REFERENCES = re.compile(r"^\s*(?:references|bibliography|литература)\s*$", re.I | re.M)
CITATION = re.compile(
    r"\bet al\.|\[[\d,\s–-]+\]|theoretical|previously|reported by|literature|simulat\w*|"
    r"\bestimat(?:e[ds]?|ion)\b|\btill now\b",
    re.I,
)
BAD_PCE = re.compile(
    r"\b(?:pseudo|external quantum|internal quantum|retention|retained|relative|improvement by)\b",
    re.I,
)
BAD_VOC = re.compile(r"\b(?:loss|deficit|difference|boost|increase\s+of|decrease\s+of)\b", re.I)


def requested_metrics(criteria):
    match = REQUEST.fullmatch(criteria.strip())
    if not match:
        return ()
    names = {"pce":"PCE", "кпд":"PCE", "voc":"Voc"}
    metrics = re.findall(METRIC, match["metrics"], re.I)
    return tuple(dict.fromkeys(names[name.casefold().replace("_","")] for name in metrics))


def plain_text(text):
    text = unicodedata.normalize("NFKC", text)
    return " ".join(re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", text).split())


def sentence_context(text, start, end):
    left, right = max(0,start-140), min(len(text),end+125)
    before = list(re.finditer(r"(?<=[.!?])\s+(?=[A-ZА-Я])",text[left:start]))
    after = re.search(r"(?<=[.!?])\s+(?=[A-ZА-Я])",text[end:right])
    if before:
        left += before[-1].end()
    if after:
        right = end+after.start()
    return text[left:right].strip()


def text_candidates(pages, metrics, located):
    seen = set()
    output = []
    for page, original in pages:
        references = REFERENCES.search(original)
        text = plain_text(original[:references.start()] if references else original)
        for metric, patterns in (("Voc",(VOC_AFTER,)), ("PCE",(PCE_AFTER,PCE_BEFORE))):
            if metric not in metrics:
                continue
            for pattern in patterns:
                for match in pattern.finditer(text):
                    context=sentence_context(text,match.start(),match.end())
                    marker=text[max(0,match.start()-20):min(len(text),match.end()+20)]
                    following=text[match.end():match.end()+14]
                    if (CITATION.search(context) or re.match(r"\s*\.\d{1,3}\b",following)
                            or (BAD_PCE if metric=="PCE" else BAD_VOC).search(marker)):
                        continue
                    raw,raw_unit=match["number"],match["unit"]
                    unit = "V" if metric=="Voc" else "%"
                    value=Decimal(raw.replace(",","."))
                    if raw_unit.casefold()=="mv":
                        value/=1000
                    if not 0<value<=(10 if metric=="Voc" else 100):
                        continue
                    key=metric,value,unit
                    # The same number can describe another device. Keep text
                    # with its distinct physical context unless the PDF repeats
                    # the exact excerpt, or the number already appears in its
                    # own table on this same page.
                    if (page,*key) in located or (key,context) in seen:
                        continue
                    seen.add((key,context))
                    output.append({"page":page,"metric":metric,"label":"","table":"","value":format(value,"f"),
                                   "raw":raw,"unit":unit,"raw_unit":raw_unit,"spread":"","aggregation":"",
                                   "fragment":raw,"evidence":context,"bbox":None})
        if references:
            break
    return output


def candidates(path, metrics):
    # The established parser handles real row/column geometry, wrapped
    # headers, cell units and statistics before Qwen receives any candidates.
    from .pdf_lab import table_records

    doc=table_records(path,list(metrics))
    output, located = [], set()
    for row in doc["records"]:
        norm=row["normalized"]
        source=row["sources"]["value"]
        if row.get("ambiguity") or not row["sample"] or not norm["value"] or not norm["unit"] or not source.get("bbox"):
            continue
        output.append({"page":row["page"],"metric":row["metric"],"label":row["sample"],"table":row["table"],
                       "value":norm["value"],"raw":row["value_raw"],"unit":norm["unit"],"raw_unit":row["unit_raw"],
                       "spread":norm.get("spread_value",""),"aggregation":row["context"].get("aggregation",{}).get("value",""),
                       "fragment":source["text"],"evidence":row["sources"]["header"]["text"],"bbox":source["bbox"],
                       "source_context":row["context"]})
        located.add((row["page"],row["metric"],Decimal(norm["value"]),norm["unit"]))
    reader=PdfReader(path)
    pages=[(i,page.extract_text() or "") for i,page in enumerate(reader.pages,1)]
    output.extend(text_candidates(pages,metrics,located))
    catalog={f"P{i:03}":entry for i,entry in enumerate(output,1)}
    return catalog,[i for i,text in pages if text.strip()],doc["image_only_pages"]

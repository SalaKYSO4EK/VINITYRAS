"""Provide numbered PDF cells for Qwen without making it retype printed numbers."""
import re


FIELDS = re.compile(r"^(?P<metric>PCE|J\s*sc|V\s*oc|FF)(?P<stat>max|mean|avg)?\s*"
                    r"(?:\((?P<unit>[^)]+)\))?$", re.I)
VALUE = re.compile(r"^(?P<number>[+−-]?(?:\d+(?:[.,]\d+)?|\.\d+))"
                   r"(?:\s*±\s*(?P<spread>\d+(?:[.,]\d+)?))?$")
TABLE = re.compile(r"^\s*((?:Table|Таблица)\s+[A-Za-z]?\d+[A-Za-z]?)(?!\w)",re.I)
METRICS = {"pce":"PCE","jsc":"Jsc","voc":"Voc","ff":"FF"}


def survey(text):
    lines = text.splitlines()
    if not lines or not (caption := TABLE.match(lines[0])):
        return {}
    for header_line, line in enumerate(lines[1:8],1):
        headings = re.split(r" {3,}",line.strip())
        metrics = [FIELDS.fullmatch(heading) for heading in headings[1:]]
        if len(metrics)>=2 and all(metrics):
            break
    else:
        return {}
    catalog = {}
    for offset,row in enumerate(lines[header_line+1:],header_line+1):
        if not row.strip():
            continue
        cells = re.split(r" {3,}",row.strip())
        if len(cells)!=len(headings):
            return {}
        label = cells[0]
        if not re.search(r"[A-Za-zА-Яа-я]",label) or len(label)>100:
            return {}
        for column, (header, raw, match) in enumerate(zip(headings[1:],cells[1:],metrics),1):
            if raw in {"–","—","-","N/A"}:
                continue
            parsed = VALUE.fullmatch(raw)
            if not parsed:
                return {}
            unit = match["unit"] or ""
            if unit not in {"%", "V", "mV", "mA/cm2", "mA/cm²", "mA cm−2", "mA cm-2"}:
                unit = ""
            metric = METRICS["".join(match["metric"].lower().split())]
            stat = (match["stat"] or "").lower()
            catalog[f"C{offset}_{column}"] = {
                "label":label,"row_quote":row.strip(),"table":caption[1],"header":header,
                "metric":metric,"number":parsed["number"],"spread":parsed["spread"] or "",
                "unit":unit,"stat":{"max":"champion","mean":"mean","avg":"mean"}.get(stat,""),
                "quote":raw}
    return catalog

"""Normalize printed photovoltaic quantities and check comparable measurements."""
from __future__ import annotations

from decimal import Decimal
import re

from article_analyzer.photovoltaic_tables import CONFIG, unit_factors, unit_family, unit_key


PIN = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mW\s*(?:/\s*cm\s*[²2]|cm\s*[−–^-]+\s*2)|W\s*(?:/\s*m\s*[²2]|m\s*[−–^-]+\s*2))")
PIN_LEGACY = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>mW\s*cm(?:/C02|\s+\(cid:1\)2))")
SPECTRUM = re.compile(r"\bAM\s*1[.,]5\s*(?:[GD]\b)?", re.I)
SUN = re.compile(r"\b(?:1|one)[\s-]+sun\b", re.I)
MODULE = re.compile(r"\b(?:(?:mini[\s-]*)?modules?|series.connected\s+cells)\b", re.I)
AGGREGATION = {
    "champion": r"\b(?:champion|best.performing)\b",
    "mean": r"\b(?:mean|averaged?)\b",
    "median": r"\bmedian\b",
}


def as_text(value):
    return "" if value is None else format(value, "f")


def amount(raw):
    raw = raw.strip().replace("−", "-").removesuffix("%").strip()
    if re.fullmatch(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?", raw):
        return Decimal(raw.replace(",", ""))
    return Decimal(raw.replace(",", "."))


def normalize(entry):
    metric = entry["metric"]
    raw = entry["value_raw"]
    unit = entry["unit_raw"]
    origin = entry["unit_origin"]
    target = CONFIG["units"][unit_family(metric)]["normalized"]
    if "%" in raw:
        if unit and unit != "%":
            return {"value": "", "unit": target, "origin": "conflicting_units",
                    "status": "needs_clarification", "alternatives": [unit, "%"]}
        unit, origin = "%", "value_cell"
    factors = unit_factors(metric)
    number = amount(raw)
    key = unit_key(unit)
    alternatives = []
    if not unit and metric == "FF" and Decimal(0) <= number <= Decimal(1):
        # A bare fill factor is dimensionless. Keep this convention inspectable.
        key, origin = "fraction", "dimensionless_convention"
        alternatives = [{"value": as_text(number), "unit": "%", "condition": "Если авторы опустили знак процента."}]
    if key not in factors:
        return {"value": "", "unit": target, "origin": origin, "status": "unit_unresolved",
                "alternatives": [], "reason": "В ячейке и заголовке нет однозначной поддерживаемой единицы."}
    if "(cid:" in unit:
        origin = "table_header_font_mapping"
    if re.fullmatch(r"[+-]?\d{1,3},\d{3}", raw) and origin != "dimensionless_convention":
        alternatives.append({"value": as_text(Decimal(raw.replace(",", ".")) * Decimal(factors[key])),
                             "unit": target, "condition": "Если запятая является десятичным разделителем."})
    factor = Decimal(factors[key])
    spread = amount(entry["spread_raw"]) * factor if entry.get("spread_raw") else None
    return {"value": as_text(number * factor), "unit": target, "spread_value": as_text(spread),
            "origin": origin, "status": "convention" if alternatives else "normalized",
            "alternatives": alternatives}


def illumination_options(source, scope):
    text = source["text"]
    for match in PIN.finditer(text):
        factor = unit_factors("Pin").get(unit_key(match["unit"]))
        if factor is not None:
            yield "pin", {"value": as_text(amount(match["value"]) * Decimal(factor)), "unit": "mW/cm2",
                          "raw_value": match["value"], "raw_unit": match["unit"],
                          "scope": scope, "source": source}
    if scope == "methods" and re.search(r"\b(?:illumination|simulator)\b", text, re.I):
        for match in PIN_LEGACY.finditer(text):
            yield "pin", {"value": as_text(amount(match["value"])), "unit": "mW/cm2",
                          "raw_value": match["value"], "raw_unit": match["unit"],
                          "unit_origin": "pdf_font_mapping", "scope": scope, "source": source}
    for match in SPECTRUM.finditer(text):
        yield "spectrum", {"value": re.sub(r"\s+", "", match[0]).upper().replace(",", "."),
                           "scope": scope, "source": source}
    for match in SUN.finditer(text):
        yield "insolation", {"value": match[0], "scope": scope, "source": source}


def table_context(entry):
    """Read only locally scoped statements in the row, caption or whole-table note."""
    fragments = [("row", entry["sources"]["sample"])]
    if entry["label_origin"] in ("printed_row", "paired_scan_row", "printed_column"):
        fragments[0] = ("row", dict(fragments[0][1], text=entry["sample"]))
    fragments += [("series", entry["sources"][name]) for name in ("caption", "note")
                  if entry["sources"].get(name)]
    options_by_key = {name: [] for name in ("pin", "spectrum", "insolation", "aggregation")}
    note_uncertainty = None
    for scope, source in fragments:
        text = source["text"]
        marker = re.match(r"\s*([*†‡])", text)
        if marker and marker[1] not in entry["sample"]:
            if any(pattern.search(text) for pattern in (PIN, SPECTRUM, SUN)):
                note_uncertainty = {"reason": "У примечания есть маркер, отсутствующий у строки.",
                                    "alternatives": ["Условие относится к помеченным строкам.",
                                                     "Связь с этой строкой не подтверждена."],
                                    "source": dict(source, page=entry["page"])}
            continue
        for name, option in illumination_options(dict(source, page=entry["page"]), scope):
            options_by_key[name].append(option)
        for aggregation, pattern in AGGREGATION.items():
            if re.search(pattern, text, flags=re.I):
                options_by_key["aggregation"].append({"value": aggregation, "scope": scope,
                                                       "source": dict(source, page=entry["page"])})
    designation = entry["sources"].get("aggregation")
    if designation:
        options_by_key["aggregation"] = [{"value": designation["value"], "scope": designation.get("scope", "row"),
                                           "n_devices": designation["n_devices"],
                                           "source": dict(designation, page=entry["page"])}]
    resolved, ambiguous = {}, {"note_scope": note_uncertainty} if note_uncertainty else {}
    for key, options in options_by_key.items():
        preferred = [o for o in options if o["scope"] in {"row", "column"}] or options
        if len({o["value"] for o in preferred}) > 1:
            ambiguous[key] = {"reason": "Несколько значений в области действия заголовка или строки.",
                              "alternatives": preferred}
        elif preferred:
            resolved[key] = preferred[0]
    raw_direction = entry["condition"]
    direction = {"forward": "forward", "reverse": "reverse"}.get(raw_direction.casefold(), "")
    module = next((dict(source, page=entry["page"]) for _, source in fragments if MODULE.search(source["text"])), None)
    return {"scan_direction": direction, "scan_direction_raw": raw_direction,
            "module_source": module,
            "scan_direction_source": entry["sources"].get("direction"), "ambiguities": ambiguous, **resolved}


def input_record(record):
    return {key: record[key] for key in ("result_id", "metric", "value_raw", "unit_raw",
                                          "normalized", "page", "table", "sources")}


def audit(rows, integrated=(), bandgaps=(), mean_rows=()):
    """Audit one measurement occurrence. Pairing of auxiliary groups must be explicit."""
    main = {r["metric"]: r for r in rows}
    context = rows[0]["context"]
    checks = []

    def check(name, inputs, state="unavailable", reason="", **details):
        checks.append({
            "name": name, "status": state, "reason": reason, "spec": CONFIG["checks"][name],
            "inputs": [input_record(r) for r in inputs], **details,
        })

    def number(metric):
        row = main.get(metric)
        if row and not row.get("ambiguity") and row["normalized"]["value"]:
            return Decimal(row["normalized"]["value"])
        return None

    jv_keys = ("PCE", "Voc", "Jsc", "FF")
    observed, voltage, current, factor = (number(key) for key in jv_keys)
    pin = context.get("pin")
    used = [main[m] for m in jv_keys if m in main]
    missing = [key for key in jv_keys if number(key) is None]
    if not pin or Decimal(pin["value"]) <= 0:
        missing.append("Pin")
    if any(r.get("measurement_type") != "J-V" for r in used):
        missing.append("однозначный общий режим ВАХ")
    formula = "PCE[%] = Voc[V] × Jsc[mA/cm2] × FF[%] / Pin[mW/cm2]"
    if context.get("aggregation", {}).get("value") in {"mean", "median"}:
        check("jv_consistency", used,
              reason="КПД для выборки нельзя проверять произведением отдельных средних или медиан.",
              formula=formula, pin=pin)
    elif missing:
        check("jv_consistency", used, reason="Нужны сопоставимые данные: " + ", ".join(missing),
              formula=formula, pin=pin)
    else:
        calculated = voltage * current * factor / Decimal(pin["value"])
        relative = abs(observed - calculated) / abs(observed) if observed else None
        limit = Decimal(CONFIG["checks"]["jv_consistency"]["relative_tolerance"])
        state = ("failed" if relative > limit else "passed") if relative is not None else "unavailable"
        check("jv_consistency", used, state, "Опубликованный PCE равен нулю." if relative is None else "",
              formula=formula, pin=pin, reported=as_text(observed), calculated=as_text(calculated),
              unit="%", relative_error=as_text(relative), relative_formula="|PCE_из_статьи − PCE_расчёт| / |PCE_из_статьи|",
              assumptions=["FF трактуется как безразмерная доля"] if main["FF"]["normalized"]["status"] == "convention" else [])

    if len(integrated) == 1 and current and integrated[0]["normalized"]["value"]:
        eqe = integrated[0]
        calculated = Decimal(eqe["normalized"]["value"])
        relative = abs(calculated - current) / abs(current)
        state = "failed" if relative > Decimal(CONFIG["checks"]["eqe_consistency"]["relative_tolerance"]) else "passed"
        check("eqe_consistency", [main["Jsc"], eqe], state, formula="|Jsc_EQE − Jsc_JV| / |Jsc_JV|",
              reported=as_text(current), calculated=as_text(calculated), unit="mA/cm2",
              relative_error=as_text(relative))
    else:
        check("eqe_consistency", [main["Jsc"]] if "Jsc" in main else [],
              reason="Нет однозначной пары Jsc по ВАХ и EQE для одного прибора и сопоставимых условий.")

    if voltage is not None and bandgaps and not context["module_source"]:
        for eg in bandgaps:
            limit = Decimal(eg["normalized"]["value"])
            check("voc_limit", [main["Voc"], eg], "passed" if voltage < limit else "failed",
                  formula="Voc < Eg/q; Eg в эВ численно равен пределу Voc в В", reported=as_text(voltage),
                  calculated=as_text(limit), unit="V")
    else:
        check("voc_limit", [main["Voc"]] if "Voc" in main else [],
              formula="Voc < Eg/q; Eg в эВ численно равен пределу Voc в В",
              reason=("В таблице упомянут модуль или последовательная сборка. Нужен Voc отдельной ячейки."
                      if context["module_source"] else "Нет Voc и подтверждённого Eg того же образца."),
              module_source=context["module_source"])

    if observed is not None and len(mean_rows) == 1 and mean_rows[0]["normalized"]["value"]:
        mean = mean_rows[0]
        limit = Decimal(mean["normalized"]["value"])
        check("champion_vs_mean", [main["PCE"], mean], "passed" if observed >= limit else "failed",
              formula="PCE лучшего прибора ≥ средний PCE по той же партии", reported=as_text(observed),
              calculated=as_text(limit), unit="%")
    else:
        check("champion_vs_mean", [main["PCE"]] if "PCE" in main else [],
              reason="Нет подтверждённой пары лучшего и среднего прибора одной партии и условий.")

    rule = CONFIG["checks"]["range"]
    check("range", [main[m] for m in ("Jsc", "FF", "PCE") if m in main],
          rule["status"], rule["reason"])
    return checks

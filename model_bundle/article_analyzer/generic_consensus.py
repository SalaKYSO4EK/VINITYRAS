"""Reconcile two independently extracted candidates against original PDF evidence."""
from collections import defaultdict
import re
import unicodedata

STATUS = "verification_status"
COUNTS = ("agreed", "resolved_from_source", "needs_review")
NUMBER = r"[+\-−]?\d+(?:[.,]\d+)?(?:[eE][+\-−]?\d+)?"


def flat(text):
    return " ".join(str(text or "").split())


def heading_key(value):
    return re.sub(r"[^\w]|_", "", unicodedata.normalize("NFKC", value).casefold())


def cell_location(cell):
    return {key: cell[key] for key in ("source_table", "source_row", "source_column", "source_bbox")}


def printed_assignment(field, proposed, evidence):
    """Locate a repeated numeric value by a unique, literally printed field label."""
    proposed = flat(proposed)
    if not re.fullmatch(NUMBER, proposed):
        return None
    label = r"[\s_\-–−]+".join(re.escape(word) for word in re.split(r"[\s_-]+", field))
    assignment = re.compile(rf"(?<![\w-]){label}\s*[:=]\s*(?P<value>{NUMBER})(?!\w|[.,]\d)", re.I)
    locations = [(page, match) for page, text in evidence.pages.items() for match in assignment.finditer(text)]
    if len(locations) != 1 or locations[0][1]["value"] != proposed:
        return None
    page, match = locations[0]
    start, stop = match.span("value")
    return {"status": "text_matched", "method": "printed_field_label", "page": page,
            "start": start, "end": stop, "quote": match["value"], "label_start": match.start()}


def source_candidate(record, check, fields, evidence, cells):
    values = {field: record.get(field, "") for field in fields}
    if not any(values.values()):
        return None
    hits = {name: hit for name, hit in check.get("fields", {}).items()
            if name in fields and hit.get("status") == "text_matched"}
    for field in values.keys() - hits.keys():
        location = printed_assignment(field, values[field], evidence)
        if location:
            hits[field] = location
    hints = {flat(value) for value in hits_as_values(record, check)}
    expected_headers = {heading_key(field) for field in fields}
    locations = {cell["row_id"] for cell in cells if cell["source_row"] in hints
                 and heading_key(cell["source_column"]) in expected_headers}
    locations = locations or {
        cell["row_id"] for cell in cells for field in hits
        if flat(values[field]) == cell["text"] and cell["source_row"] in hints - {flat(values[field])}
    }
    chosen = evidence.fragments.get(check.get("model_fragment_id"))
    if not chosen or not any(chosen.start <= hit["start"] < chosen.end for hit in hits.values()):
        first = min((hit["start"] for hit in hits.values()), default=-1)
        chosen = next((fragment for fragment in evidence.fragments.values()
                       if fragment.start <= first < fragment.end), None)
    # A valid model locator alone does not give an unsupported record a source.
    key = ("table", *next(iter(locations))) if len(locations) == 1 else (
        ("fragment", chosen.id) if chosen else None
    )
    return {"key": key, "values": values, "hits": hits, "fragment": chosen.id if chosen else None}


def hits_as_values(record, check):
    return (record.get(field, hit.get("quote", "")) for field, hit in check.get("fields", {}).items()
            if hit.get("status") == "text_matched")


def same_position(left, right):
    return (left["start"], left["end"]) == (right["start"], right["end"])


def pairing(left, right):
    """Pair duplicate-fragment records only where exact occurrences link them uniquely."""
    if len(left) <= 1 and len(right) <= 1:
        return [(left, right)]
    peers = defaultdict(set)
    reverse = defaultdict(set)
    for i, first in enumerate(left):
        for j, second in enumerate(right):
            if any(same_position(hit, second["hits"][field])
                   for field, hit in first["hits"].items() if field in second["hits"]):
                peers[i].add(j)
                reverse[j].add(i)
    pairs = [(i, next(iter(matches))) for i, matches in peers.items()
             if len(matches) == 1 and len(reverse[next(iter(matches))]) == 1]
    remaining_left = [item for i, item in enumerate(left) if all(i != start for start, _ in pairs)]
    remaining_right = [item for j, item in enumerate(right) if all(j != end for _, end in pairs)]
    result = [([left[i]], [right[j]]) for i, j in pairs]
    if remaining_left or remaining_right:
        result.append((remaining_left, remaining_right))
    return result


def verify_field(field, left, right, key, evidence, cells):
    versions = {"first": [item["values"][field] for item in left],
                "second": [item["values"][field] for item in right]}
    populated = {flat(value) for values in versions.values() for value in values if value.strip()}
    if not populated:
        return {"status": "empty", "value": ""}
    result = {"status": "needs_review", "value": "", "candidates": versions}
    if len(left) > 1 or len(right) > 1:
        return dict(result, reason="В одном фрагменте несколько записей без однозначного соответствия между проходами.")
    first = left[0]["hits"].get(field) if left else None
    second = right[0]["hits"].get(field) if right else None
    agreed = first and second and same_position(first, second)
    if key[0] == "table":
        # Match the requested field itself to a printed header, and require exactly
        # one corresponding cell in the already identified physical table row.
        column = [cell for cell in cells if cell["row_id"] == key[1:]
                  and heading_key(cell["source_column"]) == heading_key(field)]
        if len(column) == 1:
            value = column[0]["text"]
            if agreed and flat(first["quote"]) == value:
                return dict(result, status="agreed", value=first["quote"], evidence=first)
            if value in populated:
                return dict(result, status="resolved_from_source", value=value,
                            evidence=cell_location(column[0]))
            return dict(result, readback=dict(cell_location(column[0]), text=value),
                        reason="Предложенное значение не совпало с ячейкой нужной строки и столбца.")
    if agreed:
        return dict(result, status="agreed", value=first["quote"], evidence=first)
    if key[0] == "fragment":
        fragment = evidence.fragments[key[1]]
        assignments = [hit for candidate in populated
                       if (hit := printed_assignment(field, candidate, evidence))
                       and fragment.start <= hit["label_start"] and hit["end"] <= fragment.end]
        if len(assignments) == 1:
            hit = assignments[0]
            return dict(result, status="resolved_from_source", value=hit["quote"], evidence=hit)
        result["readback"] = {
            candidate: [match.location() for match in evidence.matches(candidate, fragment)[:5]]
            for candidate in sorted(populated)
        }
    return dict(result, reason="Повторная сверка не подтвердила однозначную связь поля и значения.")


def source_quote(rows, verified, evidence, number):
    labelled = [entry["evidence"] for entry in verified.values()
                if entry["status"] in {"agreed", "resolved_from_source"}
                and entry["evidence"].get("method") == "printed_field_label"]
    if len(labelled) == 1 and sum(bool(entry["value"]) for entry in verified.values()) == 1:
        hit = labelled[0]
        return evidence.pages[number][hit["label_start"]:hit["end"]]
    resolved = [entry for entry in verified.values() if entry["status"] == "resolved_from_source"]
    if resolved:
        for item in rows:
            if item["fragment"]:
                fragment = evidence.fragments[item["fragment"]]
                if all(evidence.matches(entry["value"], fragment) for entry in verified.values() if entry["value"]):
                    return fragment.text
        return resolved[0]["value"]
    exact = [entry["evidence"] for entry in verified.values()
             if entry["status"] == "agreed"]
    if exact:
        start = min(item.get("label_start", item["start"]) for item in exact)
        end = max(item["end"] for item in exact)
        if end - start <= 600:
            return evidence.pages[number][start:end]
    return next((evidence.fragments[item["fragment"]].text for item in rows if item["fragment"]), "")


def verify_group(first, second, fields, path, number, evidence, cells):
    anchored = {"first": defaultdict(list), "second": defaultdict(list)}
    unanchored = {"first": [], "second": []}
    for name, group in (("first", first), ("second", second)):
        for record, check in zip(group.get("records", []), group.get("citation_checks", []), strict=True):
            candidate = source_candidate(record, check, fields, evidence, cells)
            if candidate and candidate["key"]:
                options = anchored[name][candidate["key"]]
                if candidate not in options:
                    options.append(candidate)
            elif candidate:
                unanchored[name].append(candidate["values"])

    rows, review = [], []
    order = sorted(anchored["first"].keys() | anchored["second"].keys())
    for key in order:
        for left, right in pairing(anchored["first"].get(key, []), anchored["second"].get(key, [])):
            fields_review = {field: verify_field(field, left, right, key, evidence, cells) for field in fields}
            statuses = {item["status"] for item in fields_review.values()}
            status = next((option for option in reversed(COUNTS) if option in statuses), "")
            if not status:
                continue
            row = {field: result["value"] for field, result in fields_review.items()}
            row[STATUS] = status
            row.update(publication_id=path.stem, source_file=path.name, source_page=number,
                       source_fragment=source_quote([*left, *right], fields_review, evidence, number))
            for source_field, names in {
                "source_sample": ("source_sample", "sample", "sample_label", "device_label"),
                "source_conditions": ("source_conditions", "measurement_conditions", "conditions"),
            }.items():
                supporting = {row[name] for name in names if row.get(name)}
                if len(supporting) == 1:
                    row[source_field] = supporting.pop()
            if key[0] == "table":
                table_cells = [cell for cell in cells if cell["row_id"] == key[1:]]
                row.update(source_table=table_cells[0]["source_table"], source_row=table_cells[0]["source_row"])
                locations = [cell for cell in table_cells if any(
                    heading_key(field) == heading_key(cell["source_column"]) and result["value"] == cell["text"]
                    for field, result in fields_review.items()
                )]
                if len(locations) == 1:
                    row.update(cell_location(locations[0]))
            rows.append(row)
            review.append({"source_key": key, "status": status, "fields": fields_review})
    return rows, {"rows": review, "unanchored": unanchored}

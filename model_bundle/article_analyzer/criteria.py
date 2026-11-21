"""Turn request-specific criteria into a JSON plan and NuExtract templates."""
from __future__ import annotations

from collections import Counter
import json
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from .evidence import EvidenceIndex, Fragment, render_fragments, source_fragments
from .local_model import ExtractionError, LocalNuExtract, _reject_constant, _unique_object


Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_]*$", max_length=80)]
NonEmpty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
SOURCE_FIELDS = ("source_page", "source_fragment")
FRAGMENT_FIELD = "source_fragment_id"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class FieldSpec(StrictModel):
    name: Identifier
    instruction: str = ""
    unit: str = ""
    instruction_options: list[NonEmpty] = Field(default_factory=list)
    unit_options: list[NonEmpty] = Field(default_factory=list)

    def alternatives(self, attribute: str) -> list[str]:
        return list(dict.fromkeys(
            value.strip() for value in [getattr(self, attribute), *getattr(self, attribute + "_options")]
            if value.strip()
        ))

    def resolved(self) -> dict:
        field = self.model_dump()
        for key in ("instruction", "unit"):
            options = self.alternatives(key)
            field[key] = options[0] if len(options) == 1 else ""
            field[key + "_options"] = options
        field["status"] = ("needs_clarification" if any(
            len(field[key + "_options"]) > 1 for key in ("instruction", "unit")
        ) else "recorded")
        return field


class Group(StrictModel):
    name: Identifier
    row: NonEmpty
    fields: Annotated[list[Identifier | FieldSpec], Field(min_length=1)]

    def field_specs(self) -> list[FieldSpec]:
        return [FieldSpec(name=field) if isinstance(field, str) else field for field in self.fields]


RuleKind = Literal["scope", "normalization", "comparison", "validation", "other"]
RULE_LABELS = [*get_args(RuleKind), "skip", "review"]


class Rule(StrictModel):
    kind: RuleKind
    fragment_id: NonEmpty
    missing: str = ""
    candidate_page: int | None = None


class Interpretation(StrictModel):
    groups: list[Group]
    rules: list[Rule]
    questions: list[NonEmpty]


class FieldPage(StrictModel):
    groups: list[Group]


class InterpretationError(ValueError):
    def __init__(self, message: str, raw: str = ""):
        super().__init__(message)
        self.raw = raw


@dataclass(frozen=True)
class Source:
    name: str
    sha256: str
    pages: dict[int, str]

    def text(self) -> str:
        return "\n\n".join(f"[PAGE {page}]\n{text}" for page, text in self.pages.items())


def parse_interpretation(raw: str) -> Interpretation:
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw.strip(), flags=re.DOTALL)
    try:
        data = json.loads(fenced[1] if fenced else raw, object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
        return Interpretation.model_validate(data)
    except ValueError as error:
        raise InterpretationError(f"Invalid criteria JSON: {error}", raw) from error

def merge_page_interpretations(responses: dict[int, str]) -> str:
    groups: dict[str, Group] = {}
    rules: dict[tuple[str, str, int | None], Rule] = {}
    questions: list[str] = []
    for page, raw in responses.items():
        try:
            interpretation = parse_interpretation(raw)
        except InterpretationError as error:
            raise InterpretationError(f"Criteria page {page}: {error}", raw) from error
        questions.extend(interpretation.questions)
        for group in interpretation.groups:
            previous = groups.get(group.name)
            if previous and incompatible_group_names(previous, group):
                stem = f"{group.name[:65]}_p{page}"
                candidate = stem
                counter = 2
                while candidate in groups:
                    candidate = f"{stem}_{counter}"
                    counter += 1
                questions.append(f"Page {page}: {group.name} and {candidate} share a label but have different fields. "
                                 "Clarify whether they represent the same records.")
                group = group.model_copy(update={"name": candidate})
            if group.name in groups:
                previous = groups[group.name]
                fields = {field.name: field for field in previous.field_specs()}
                for field in group.field_specs():
                    existing = fields.get(field.name)
                    if not existing:
                        fields[field.name] = field
                        continue
                    for key in ("instruction", "unit"):
                        options = list(dict.fromkeys(existing.alternatives(key) + field.alternatives(key)))
                        setattr(existing, key, options[0] if len(options) == 1 else "")
                        setattr(existing, key + "_options", options)
                        if len(options) > 1:
                            questions.append(f"Page {page}: reconcile {key} for {group.name}.{field.name}.")
                previous.fields = list(fields.values())
            else:
                groups[group.name] = group
        for rule in interpretation.rules:
            key = (rule.kind, rule.fragment_id, rule.candidate_page)
            if key not in rules or rule.missing:
                rules[key] = rule
    return Interpretation(groups=list(groups.values()), rules=list(rules.values()),
                          questions=list(dict.fromkeys(questions))).model_dump_json()


def listed_identifiers(text: str) -> set[str]:
    return set(re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", text))


def field_names(groups: list[Group]) -> set[str]:
    return {field.name for group in groups for field in group.field_specs()}


def incompatible_group_names(first: Group, second: Group) -> bool:
    left, right = field_names([first]), field_names([second])
    common = left & right
    return bool(left - right and right - left and all(name.endswith("_id") for name in common))


def documented_field_definitions(name: str, source: Source) -> list[dict]:
    pattern = re.compile(rf"(?<!\w){re.escape(name)}\s*\(([^()]+)\)")
    return [
        {"page": page, "start": match.start(), "end": match.end(),
         "quote": match[0], "description": " ".join(match[1].split())}
        for page, text in source.pages.items() for match in pattern.finditer(text)
    ]


_NORMALIZATION_LINE = re.compile(r"(?im)^[ \t]*(?:нормализация|normalization)[ \t]*[:.\-–—]?[ \t]+([^\r\n]+)")
_UNIT_PAIR = re.compile(r"\s*([^;:—–]+?)\s+[—–]\s+([^;:—–]+?)\s*")


def declared_units(source: Source) -> list[dict]:
    """Read explicit subject — unit lists under a normalization heading."""
    units = {}
    for page, text in source.pages.items():
        for line in _NORMALIZATION_LINE.finditer(text):
            for part in line[1].rstrip(". ").split(";"):
                pair = _UNIT_PAIR.fullmatch(part)
                if not pair:
                    continue
                location = {"page": page, "start": line.start(1), "end": line.end(1), "quote": line[1]}
                for subject in pair[1].split(","):
                    name = subject.strip()
                    if name:
                        units.setdefault(name, {}).setdefault(pair[2].strip(), []).append(location)
    return [
        {"subject": subject, "unit": next(iter(choices)) if len(choices) == 1 else "",
         "unit_options": [{"unit": unit, "sources": sources} for unit, sources in choices.items()],
         "status": "recorded" if len(choices) == 1 else "needs_clarification"}
        for subject, choices in units.items()
    ]


_RULE_WORDING = {
    "normalization": re.compile(r"(?:^|\n)\s*(?:нормализация|normalization)\b|"
                               r"пересч[её]т[^.]{0,150}единиц|convert[^.]{0,110}(?:units|percentages)", re.I),
    "comparison": re.compile(r"\b(?:сопоставление|сравниваются)\b|"
                            r"(?:эксперименты|варианты метрик)[^.]{0,110}не\s+объединяются|"
                            r"\bcompare (?:experiments|results)\b", re.I),
    "validation": re.compile(r"значение\s+проверяется|проверка\s+по\s+допустимой|"
                            r"check[^.]{0,100}(?:allowed range|scale)", re.I),
}


def rule_interpretation(rule: Rule, fragment: Fragment | None) -> dict:
    """Flag conflicts with explicit wording without treating the model's label as verified."""
    cues = [kind for kind, pattern in _RULE_WORDING.items() if fragment and pattern.search(fragment.text)]
    options = list(dict.fromkeys([rule.kind, *cues]))
    needs_review = not fragment or bool(rule.missing) or len(options) > 1
    return {
        "interpretation_status": "needs_clarification" if needs_review else "model_candidate",
        "kind_options": options,
        "wording_checks": cues,
    }


def compile_interpretation(raw: str, source: Source) -> dict:
    """A model proposes the task. Code validates its structure and builds templates."""
    interpretation = parse_interpretation(raw)
    if not interpretation.groups:
        raise InterpretationError("No extraction fields were identified in the supplied criteria", raw)

    groups = []
    templates = {}
    for group in interpretation.groups:
        if group.name in templates:
            raise InterpretationError(f"Duplicate group: {group.name}", raw)
        fields = group.field_specs()
        names = [field.name for field in fields]
        if len(names) != len(set(names)):
            raise InterpretationError(f"Duplicate field in group: {group.name}", raw)
        model_fields = [name for name in names if name not in SOURCE_FIELDS]
        templates[group.name] = [dict.fromkeys([*model_fields, FRAGMENT_FIELD], "")]
        compiled_fields = []
        for field in fields:
            entry = field.resolved()
            statements = documented_field_definitions(field.name, source)
            if statements:
                entry["documented_definitions"] = statements
                descriptions = list(dict.fromkeys(item["description"] for item in statements))
                if not entry["instruction_options"]:
                    entry["instruction_options"] = descriptions
                    entry["instruction"] = descriptions[0] if len(descriptions) == 1 else ""
                if len(descriptions) > 1:
                    entry["status"] = "needs_clarification"
                    entry["instruction"] = ""
            compiled_fields.append(entry)
        groups.append({"name": group.name, "row": group.row, "fields": compiled_fields})

    fragments = source_fragments(source.pages)
    rules = []
    for rule in interpretation.rules:
        fragment = fragments.get(rule.fragment_id)
        if fragment and rule.candidate_page is not None and fragment.page != rule.candidate_page:
            fragment = None
        status = ("fragment_not_found" if fragment is None else
                  "needs_clarification" if rule.missing else "recorded")
        rules.append({**rule.model_dump(), "quote": fragment.text if fragment else "",
                      "source_pages": [fragment.page] if fragment else [],
                      "source": fragment.location() if fragment else None, "status": status,
                      "quote_status": "matched" if fragment else "missing",
                      **rule_interpretation(rule, fragment)})

    named_fields = listed_identifiers(source.text())
    parsed_fields = {field["name"] for group in groups for field in group["fields"]}
    return {
        "schema_version": 3,
        "criteria_source": {"name": source.name, "sha256": source.sha256, "pages": list(source.pages)},
        "extraction": {"groups": groups, "nuextract_template": templates},
        "unit_requirements": declared_units(source),
        "rules": rules,
        "questions": interpretation.questions,
        "coverage": {"explicit_identifiers_not_in_fields": sorted(named_fields - parsed_fields)},
    }


RULES_PROMPT = """Classify every numbered fragment from these scientific criteria.
Return a JSON object mapping each fragment ID to one label, for example:
{"P1-0":"skip","P1-25":"validation","P1-110":"normalization"}
Give each supplied ID one label:
- validation: checks of a result, e.g. checking whether a metric is within its allowed scale.
- comparison: conditions for comparing experiments, e.g. both must use the same dataset.
- normalization: conversion of values or units, e.g. conversion of fractions to percentages.
- scope: inclusion or exclusion of studies.
- other: operational requirements such as keeping a separate issue record.
- skip: headings, field lists, requests that only say which facts to extract, and ordinary prose.
- review: a requirement is ambiguous, incomplete, or explicitly refers to missing details.
Ignore standalone headings and field lists even when they contain words such as result or metric.
"""

FIELDS_PROMPT = """Read this page of scientific extraction requirements and list all explicitly specified fields.
Return only JSON of this form: {"groups":[{"name":"items","row":"one item","fields":["item_id","item_name"]}]}.
Copy each field identifier exactly, even if it appears at the end of a line or occurs in multiple lists.
Keep fields next to the other fields belonging to the same object. Include every named column from each
object, including identifiers and source fields. Do not treat explanatory prose or rule statements as fields.
Use clear snake_case group names. For criteria in ordinary prose, keep the facts requested by the user.
When a definition or unit is explicitly given for a particular field, use an object with keys name,
instruction and unit. Otherwise use the field name as a string. Never supply a unit from general knowledge.
Use an empty groups list if the page only contains headings, rules or context.
"""


def is_rules_page(text: str) -> bool:
    heading = " ".join(text.splitlines()[:2]).casefold()
    return bool(re.search(r"правила проверки|validation rules|verification rules|checking rules", heading))


_STANDALONE_HEADING = re.compile(
    r"(?:предметная область\s+\d+|subject area\s+\d+|правила проверки|validation rules|"
    r"результат проверки|validation result|verification result|нормализация|normalization)[.:!?]?",
    re.IGNORECASE,
)


def rule_fragments(source: Source) -> dict[str, Fragment]:
    """Treat exact section labels as layout and keep the body text at its original offsets."""
    return {fragment_id: fragment for fragment_id, fragment in source_fragments(source.pages).items()
            if not _STANDALONE_HEADING.fullmatch(" ".join(fragment.text.split()))}


def parse_field_page(raw: str) -> Interpretation:
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", raw.strip(), flags=re.DOTALL)
    try:
        record = json.loads(fenced[1] if fenced else raw, object_pairs_hook=_unique_object,
                            parse_constant=_reject_constant)
        if isinstance(record, dict) and isinstance(record.get("groups"), list):
            for group in record["groups"]:
                if not isinstance(group, dict) or not isinstance(group.get("fields"), list):
                    continue
                group["fields"] = [
                    field["item_id"] if isinstance(field, dict) and field.keys() == {"item_id", "item_name"}
                    and isinstance(field["item_id"], str) and field["item_id"] == field["item_name"] else field
                    for field in group["fields"]
                ]
        page = FieldPage.model_validate(record)
    except ValueError as error:
        raise InterpretationError(f"Invalid extraction field list: {error}", raw) from error
    return Interpretation(groups=page.groups, rules=[], questions=[])


def name_field_groups(groups: list[Group]) -> list[Group]:
    """Keep separately extracted lists distinct when the model repeats its example name."""
    counts = Counter(group.name for group in groups)
    used = {group.name for group in groups if counts[group.name] == 1 and group.name != "items"}
    result = []
    for group in groups:
        if counts[group.name] == 1 and group.name != "items":
            result.append(group)
            continue
        anchor = group.field_specs()[0].name.removesuffix("_id")
        name = anchor
        suffix = 2
        while name in used:
            name = f"{anchor[:70]}_{suffix}"
            suffix += 1
        used.add(name)
        row = f"one {name.replace('_', ' ')}" if group.row == "one item" else group.row
        result.append(group.model_copy(update={"name": name, "row": row}))
    return result


def parse_rule_labels(raw: str, fragments: dict[str, Fragment]) -> Interpretation:
    try:
        labels = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if not isinstance(labels, dict) or labels.keys() != fragments.keys():
            raise ValueError("Expected one label per supplied fragment ID")
        if any(label not in RULE_LABELS for label in labels.values()):
            raise ValueError("Unexpected fragment label")
    except ValueError as error:
        raise InterpretationError(f"Invalid rules-page classification: {error}", raw) from error
    rules = []
    for fragment_id, fragment in fragments.items():
        label = labels[fragment_id]
        if label == "skip":
            continue
        rules.append(Rule(kind="other" if label == "review" else label, fragment_id=fragment_id,
                          missing="Fragment classification requires review" if label == "review" else "",
                          candidate_page=fragment.page))
    return Interpretation(groups=[], rules=rules, questions=[])


class LocalCriteriaInterpreter:
    """Run the quantized instruction model and release its memory after each request."""

    def __init__(self, model_path: Path, executable: Path, threads: int):
        if threads < 1:
            raise ValueError("threads must be positive")
        self.model_path = model_path.resolve()
        self.executable = executable.resolve()
        if not self.model_path.is_file() or not self.executable.is_file():
            raise ValueError("Missing GGUF weights or llama.cpp runtime. Run tools/setup_criteria_model.py.")
        self.threads = threads
        self.raw_responses: list[dict] = []

    def interpret(self, source: Source, max_new_tokens: int = 3072) -> dict:
        started = time.perf_counter()
        self.raw_responses.clear()
        responses = {}
        page_errors = []
        field_pages = []
        retried_fields = []
        peak_rss = 0
        for page, text in source.pages.items():
            page_source = Source(source.name, source.sha256, {page: text})
            interpretation = Interpretation(groups=[], rules=[], questions=[])
            dedicated_rules = (source.name.lower().endswith(".pdf") and is_rules_page(text)
                               and not listed_identifiers(text))
            phases = ("rules",) if dedicated_rules else ("fields", "rules")
            for phase in phases:
                try:
                    raw, rss = self._generate(page_source, max_new_tokens, fields_only=phase == "fields")
                    parsed = parse_interpretation(raw)
                    peak_rss = max(peak_rss, rss)
                except InterpretationError as error:
                    page_errors.append({"page": page, "phase": phase, "error": str(error), "raw_response": error.raw})
                    continue
                if phase == "fields":
                    field_pages.append(page)
                    expected = listed_identifiers(text)
                    observed = expected & field_names(parsed.groups)
                    missing = expected - observed
                    if missing:
                        try:
                            repaired_raw, rss = self._generate(page_source, max_new_tokens, fields_only=True,
                                                               missing_fields=sorted(missing))
                            peak_rss = max(peak_rss, rss)
                            repaired = parse_interpretation(repaired_raw)
                            if (expected & field_names(repaired.groups)) > observed:
                                parsed.groups = repaired.groups
                                retried_fields.append(page)
                        except InterpretationError as error:
                            interpretation.questions.append(f"Page {page}: {error}")
                    interpretation.groups = name_field_groups(parsed.groups)
                else:
                    interpretation.rules = parsed.rules
                interpretation.questions.extend(parsed.questions)
            responses[page] = interpretation.model_dump_json()

        combined = merge_page_interpretations(responses)
        profile = compile_interpretation(combined, source)
        profile["interpreter"] = {"model": self.model_path.name, "runtime": "llama.cpp",
                                  "seconds": round(time.perf_counter() - started, 3),
                                  "peak_working_set_bytes_sampled": peak_rss}
        if page_errors:
            profile["page_errors"] = page_errors
        profile["coverage"]["plain_text_field_pages"] = field_pages
        profile["coverage"]["pages_recovered_with_field_prompt"] = retried_fields
        return profile

    def _generate(self, source: Source, max_new_tokens: int, *, fields_only: bool = False,
                  missing_fields: list[str] | None = None) -> tuple[str, int]:
        import psutil

        if not 1 <= max_new_tokens < 8192:
            raise ValueError("Criteria token budget must be between 1 and 8191")
        page = next(iter(source.pages))
        task = FIELDS_PROMPT if fields_only else RULES_PROMPT
        if fields_only and not source.name.lower().endswith(".pdf"):
            task += ("\nThe request names the exact fields to extract. Do not expand it into a standard schema. "
                     "If it asks for only a title, return only the title field, without other bibliography fields.\n")
        if fields_only and missing_fields:
            task += ("\nThe prior field list missed these explicit identifiers. Return the full field schema, "
                     "including the original fields and these exact names: " + ", ".join(missing_fields) + ".\n")
        fragments = {} if fields_only else rule_fragments(source)
        if not fields_only and not fragments:
            return Interpretation(groups=[], rules=[], questions=[]).model_dump_json(), 0
        criteria = (source.text() if fields_only else render_fragments(fragments)).replace("<|", "< |")
        prompt = (f"<|im_start|>system\n{task}<|im_end|>\n"
                  f"<|im_start|>user\n{criteria}<|im_end|>\n<|im_start|>assistant\n")
        peak_rss = 0
        started = time.perf_counter()
        # llama.cpp's Windows command-line parser can mangle Cyrillic paths.
        # CreateProcess sets the working directory in Unicode, so the model and
        # prompt can be passed as paths relative to it.
        with TemporaryDirectory(prefix="criteria-", dir=self.model_path.parent) as directory:
            prompt_file = Path(directory) / "prompt.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            args = [str(self.executable), "-m", self.model_path.name,
                    "-f", str(prompt_file.relative_to(self.model_path.parent)),
                    "-t", str(self.threads), "-ngl", "0", "-c", "8192", "-n", str(max_new_tokens),
                    "--temp", "0", "--no-display-prompt", "--no-context-shift", "-no-cnv"]
            if not fields_only:
                label_schema = {"type": "string", "enum": RULE_LABELS}
                schema = {"type": "object", "properties": dict.fromkeys(fragments, label_schema),
                          "required": list(fragments), "additionalProperties": False}
                schema_file = Path(directory) / "rules.schema.json"
                schema_file.write_text(json.dumps(schema), encoding="utf-8")
                args.extend(["-jf", str(schema_file.relative_to(self.model_path.parent))])
            with (Path(directory) / "response.txt").open("w+b") as output, (
                Path(directory) / "runtime.log"
            ).open("w+b") as log:
                with subprocess.Popen(args, cwd=self.model_path.parent,
                                      stdin=subprocess.DEVNULL, stdout=output, stderr=log,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) as process:
                    try:
                        running = psutil.Process(process.pid)
                        while process.poll() is None:
                            memory = running.memory_info()
                            peak_rss = max(peak_rss, memory.rss)
                            if time.perf_counter() - started > 600:
                                process.kill()
                                raise InterpretationError("The local criteria interpreter exceeded 10 minutes")
                            time.sleep(0.2)
                    except psutil.NoSuchProcess:
                        pass
                    except BaseException:
                        process.kill()
                        raise
                    returncode = process.wait()
                output.seek(0)
                raw = output.read().decode("utf-8", errors="replace").strip()
                raw = raw.removesuffix(" [end of text]").strip()
                self.raw_responses.append({"page": page, "mode": "fields" if fields_only else "rules",
                                           "raw_output": raw})
                if returncode:
                    log.seek(0)
                    detail = log.read().decode("utf-8", errors="replace").splitlines()
                    raise InterpretationError("llama.cpp failed: " + "\n".join(detail[-8:]), raw)

        interpretation = parse_field_page(raw) if fields_only else parse_rule_labels(raw, fragments)
        for rule in interpretation.rules:
            rule.candidate_page = page
        return interpretation.model_dump_json(), peak_rss


def ground_article_record(raw: dict, evidence: EvidenceIndex, index: int) -> tuple[dict, dict]:
    proposed = raw.get(FRAGMENT_FIELD, "").strip()
    selected = evidence.fragments.get(proposed)
    facts = {key: value for key, value in raw.items() if key not in (*SOURCE_FIELDS, FRAGMENT_FIELD)}
    fields = {key: evidence.locate(value, selected) for key, value in facts.items()}
    populated = [entry for entry in fields.values() if entry["status"] != "empty"]
    matched = [entry for entry in populated if entry["status"] == "text_matched"]
    pages = {entry.get("page") for entry in populated}
    page, quote = "", ""
    if len(pages) == 1 and None not in pages:
        page_number = pages.pop()
        page = str(page_number)
    if page and len(populated) == len(matched):
        start = min(entry["start"] for entry in matched)
        end = max(entry["end"] for entry in matched)
        if end - start <= 600:
            quote = evidence.pages[page_number][start:end]
    record = {**facts, "source_page": page, "source_fragment": quote}
    check = {"record": index, "source_page": page, "quote_found_on_page": bool(quote),
             "model_fragment_id": proposed, "model_fragment_available": selected is not None,
             "fields": fields}
    return record, check


def field_guidance(group: dict) -> str:
    lines = []
    for field in group["fields"]:
        if field["name"] in (*SOURCE_FIELDS, FRAGMENT_FIELD):
            continue
        guidance = []
        if field["instruction"]:
            guidance.append(field["instruction"])
        if field.get("unit"):
            guidance.append(f"Criteria specify unit {field['unit']}. Preserve the reported value and unit.")
        if guidance:
            lines.append(f"{field['name']}: {' '.join(guidance)}")
    return "\n".join(lines)


def exact_model_fields(profile: dict) -> None:
    """For a strict request, ground values in code without adding locator fields."""
    extraction = profile["extraction"]
    extraction["exact_model_fields"] = True
    for group in extraction["groups"]:
        prototype = extraction["nuextract_template"][group["name"]][0]
        if not any(field["name"] == FRAGMENT_FIELD for field in group["fields"]):
            prototype.pop(FRAGMENT_FIELD, None)


def extract_with_profile(client: LocalNuExtract, profile: dict, article: Source,
                         groups: list[str] | None, max_new_tokens: int) -> dict:
    extraction = profile["extraction"]
    templates = extraction["nuextract_template"]
    requested = set(groups or templates)
    if requested - templates.keys():
        raise ValueError(f"Unknown groups: {', '.join(sorted(requested - templates.keys()))}")

    evidence = EvidenceIndex(article.pages)
    article_text = render_fragments(evidence.fragments)
    strict = extraction.get("exact_model_fields", False)
    results = {}
    for group in extraction["groups"]:
        name = group["name"]
        if name not in requested:
            continue
        started = time.perf_counter()
        definitions = field_guidance(group)
        template = {name: templates[name]}
        has_locator = FRAGMENT_FIELD in templates[name][0]
        source_instruction = (
            "Copy the evidence fragment ID into source_fragment_id without brackets. Leave it empty if unknown."
            if has_locator else "Return only the fields in the template."
        )
        prompt = (
            "Extraction instructions (not article evidence). "
            f"One output record represents: {group['row']}\n{definitions}\n"
            "Use only the article below as evidence. Copy stated values, use empty strings for missing "
            f"values. {source_instruction}\n\n"
            f"ARTICLE\n{article_text if has_locator else article.text()}"
        )
        attempts = []
        try:
            answer = client.extract(prompt, template, max_new_tokens=max_new_tokens)
        except ExtractionError as error:
            attempts.append({"template": template, "raw_output": error.raw_output, "error": str(error)})
            if strict or error.raw_output.strip():
                results[name] = {"error": str(error), "raw_output": error.raw_output,
                                 "attempts": attempts, "source_verified": False}
                continue
            # Some NuExtract releases stop immediately on the fragment-ID prompt.
            # Retry the established extraction format and recover only literal locations.
            previous_fields = dict(templates[name][0])
            previous_fields.pop(FRAGMENT_FIELD)
            previous_fields.update(dict.fromkeys(SOURCE_FIELDS, ""))
            template = {name: [previous_fields]}
            previous_prompt = (
                "Extraction instructions (not article evidence). "
                f"One output record represents: {group['row']}\n{definitions}\n"
                "Use only the article below as evidence. Copy stated values, use empty strings for missing "
                "values, and copy an exact article quotation and PDF page for each record.\n\n"
                f"ARTICLE\n{article.text()}"
            )
            try:
                answer = client.extract(previous_prompt, template, max_new_tokens=max_new_tokens)
            except ExtractionError as retry_error:
                attempts.append({"template": template, "raw_output": retry_error.raw_output, "error": str(retry_error)})
                results[name] = {"error": str(retry_error), "raw_output": retry_error.raw_output,
                                 "first_error": str(error), "attempts": attempts, "source_verified": False}
                continue
            recovered = True
        else:
            recovered = False
        attempts.append({"template": template, "raw_output": answer.raw_output})
        model_records = answer.data[name]
        grounded = [ground_article_record(row, evidence, index) for index, row in enumerate(model_records, 1)]
        results[name] = {"records": [record for record, _ in grounded], "model_records": model_records,
                         "attempts": attempts,
                         "citation_checks": [check for _, check in grounded],
                         "seconds": round(answer.seconds, 3), "recovered_from_empty_model_output": recovered,
                         "wall_seconds": round(time.perf_counter() - started, 3),
                         "source_verified": False}
    return {"article": article.name, "sha256": article.sha256, "pages": list(article.pages),
            "model_revision": client.revision, "groups": results}

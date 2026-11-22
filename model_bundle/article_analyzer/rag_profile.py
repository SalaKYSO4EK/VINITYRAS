"""Build the participant RAG tables from PDFs; gold data is never an extraction input."""
import csv
import hashlib
import json
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re

import pdfplumber

from article_analyzer.rag_documents import (
    TASK, TOP_K, bibliography, compact, flat, passages, read_pages, scope_evidence,
)
from article_analyzer.rag_tables import extract_table_cells

SCHEMA = json.loads((Path(__file__).resolve().parents[1] / "config/rag-schema.json").read_bytes())
TABLE = re.compile(r"\bTable\s+(?:[A-Z]?\d+|[IVX]+)\b", re.I)
QA = re.compile(r"\b(?:(?:open[ -]domain|multi[ -]hop|knowledge[ -]intensive|long[ -]form|conversational)\s+)*QA\b")
OWN = re.compile(r"\b(?:we (?:also |further )?(?:evaluat|conduct|experiment|compar|achiev|obtain|test|report|measure|implement|run|reproduc|use)|our (?:results|experiments|evaluation))", re.I)
BORROWED = re.compile(r"(?:results|numbers|scores)\s+(?:are |were )?(?:copied|adopted|taken|quoted|obtained directly|reported)\s+from|as reported (?:in|by)|taken from (?:the )?(?:original|previous)", re.I)
NUMBER = re.compile(r"[+\-−]?(?:\d+(?:\.\d+)?|\.\d+)")


def recognize_task(text):
    matches = [m.group() for m in TASK.finditer(text)]
    matches += [re.sub(r"\bQA\b","question answering",m.group()) for m in QA.finditer(text)]
    types = {flat(m).lower().replace("multi hop","multi-hop").replace("open domain","open-domain") for m in matches}
    specific = [v for v in types if not any(w != v and w.endswith(v) for w in types)]
    return specific[0] if len(specific) == 1 else ""


def text_document(path, ocr_dir=None):
    meta, pages = read_pages(path)
    with path.open("rb") as stream:
        sha = hashlib.file_digest(stream, "sha256").hexdigest()
    native = list(pages)
    ocr = {}
    if ocr_dir and not any(t.strip() for t in pages):
        cached = Path(ocr_dir) / (path.stem + ".json")
        if cached.exists():
            candidate = json.loads(cached.read_bytes())
            if candidate.get("source_sha256") == sha:
                ocr = candidate
                for number,text in ocr.get("pages",{}).items():
                    if 1 <= int(number) <= len(pages):
                        pages[int(number)-1] = text
    errors, cells, checked = [], [], []
    with pdfplumber.open(path) as pdf:
        bib,sources = bibliography(meta, pdf.pages[0] if native[0].strip() else None, pages[0])
        if ocr.get("title") and ocr.get("title_source"):
            bib["title"] = ocr["title"]
            sources["title"] = {"value":ocr["title"], "page":1, "method":"visually_verified_scan_title",
                                "source":ocr["title_source"]}
        for i,text in enumerate(native,1):
            if not TABLE.search(text):
                continue
            page = pdf.pages[i-1]
            try:
                extracted = extract_table_cells(page,i)
                cells.extend(extracted)
                checked.append(i)
            except (ValueError, OSError, ArithmeticError, RuntimeError) as exc:
                errors.append({"page":i, "error":str(exc)})
            finally:
                page.close()
    return {"sha256":sha, "file":path.name, "pages":len(pages), "bib":bib, "bib_sources":sources,
            "scope":scope_evidence(pages), "text_pages":sum(bool(t.strip()) for t in native),
            "ocr_pages":sorted(ocr.get("pages",{})), "tested_table_pages":checked,
            "cells":cells, "errors":errors}, pages


def table_task(cell, sentences):
    caption = cell["caption_fragment"]
    label = recognize_task(caption)
    if label:
        return label, f"p. {cell['page']}, {caption}"
    dataset = cell["dataset"]
    if len(dataset) < 3:
        return "", ""
    found = {}
    for number,sentence in sentences:
        if re.search(r"(?<!\w)"+re.escape(dataset)+r"(?!\w)",sentence,re.I):
            label = recognize_task(sentence)
            if label:
                found.setdefault(label,f"p. {number}, {sentence}")
    return next(iter(found.items())) if len(found)==1 else ("","")


def project(path, ocr_dir=None):
    doc,pages = text_document(path, ocr_dir)
    tables = {t:[] for t in SCHEMA["tables"]}
    pid = path.stem
    indices = {t:{} for t in tables}

    def add(table, row):
        record = {"publication_id":pid, **row}
        tables[table].append(record)
        return record

    def entity(table, key, prefix, data):
        if not key:
            return ""
        ids = indices[table]
        if key not in ids:
            ids[key] = prefix + str(len(ids)+1).zfill(3)
            add(table,{SCHEMA["keys"][table]:ids[key],**data})
        return ids[key]

    scope = doc["scope"]
    in_scope = "true" if set(scope) == {"retrieval","evaluation"} else ""
    add("publications",{**doc["bib"], "in_scope":in_scope})
    if not in_scope:
        doc["errors"].append({"reason":"Область применения требует чтения статьи; автоматического решения нет."})
    sentences = list(passages(pages))
    task_links = {}
    unresolved = []
    confirmed = []
    origin_by_table = {}
    for cell in doc.pop("cells"):
        n, table, raw = cell["page"], cell["table"], cell["raw"]
        context = (n,table,cell["caption_fragment"])
        if context not in origin_by_table:
            cited = BORROWED.search(cell["caption_fragment"]) or BORROWED.search(pages[n-1])
            nearby = " ".join(pages[max(0,n-3):min(n+1,len(pages))])
            author = OWN.search(nearby)
            origin_by_table[context] = bool(author and not cited)
        if not in_scope or not origin_by_table[context]:
            unresolved.append({**cell,"reason":"Требует проверки области или происхождения результата (primary/cited)."})
            continue
        value_match=NUMBER.match(raw)
        if not value_match or "," in raw or "e" in raw[value_match.end():].lower():
            unresolved.append({**cell,"reason":"Нет однозначного исходного числа."})
            continue
        try:
            value = format(Decimal(value_match[0].replace("−","-")), "f")
        except InvalidOperation:
            unresolved.append({**cell,"reason":"Не удалось прочитать число."})
            continue
        exp = entity("experiments",context,"E",{"evaluation_protocol":cell["caption_fragment"],
            "note":f"PDF p. {n}, {table}. Table-level grouping."})
        source = f"PDF p. {n}, {table}: {cell['row_fragment']} | {cell['header_fragment']}"
        cfg = entity("configurations",(context,cell["sample"],cell["generator"]),"C",{
            "rag_variant":cell["sample"],"generator_model":cell["generator"],
            "note":source + (". "+cell["generator_note"] if cell.get("generator_note") else "")})
        ds = entity("datasets",(context,cell["dataset"]) if cell["dataset"] else None,"DS",{
            "dataset_name":cell["dataset"],"note":source})
        task_key = (context,cell["dataset"])
        if task_key not in task_links:
            task_links[task_key] = table_task(cell,sentences)
        task, note = task_links[task_key]
        task_id = entity("tasks",task,"T",{"task_type":task,"note":note})
        cutoff = re.search(r"@(\d+)(?![\d.])\b",cell["metric"])
        unit = cell["unit"]
        r = add("results",{"result_id":"R"+str(len(tables["results"])+1).zfill(5),
            "experiment_id":exp,"config_id":cfg,"dataset_id":ds,"task_id":task_id,
            "metric_name":cell["metric"],"metric_k":cutoff[1] if cutoff else "",
            "aggregation":cell.get("aggregation",""),
            "raw_value":raw,"raw_unit":unit,"metric_value":value,"metric_unit":unit,
            "normalized_value":value if unit else "","normalized_unit":unit,
            "result_origin":"primary","source_page":str(n),"source_table":table,
            "source_fragment":cell["fragment"],"extraction_status":"extracted","note":source})
        confirmed.append({"result_id":r["result_id"], **cell})
    for number,sentence in sentences:
        for match in TOP_K.finditer(sentence):
            label=match[1] or match[2]
            entity("configurations",("explicit_top_k",number,match[0],sentence),"C",{
                "top_k":label, "note":f"PDF p. {number}. {sentence}"})
    for reason in sorted({item["reason"] for item in unresolved}):
        items = [item for item in unresolved if item["reason"]==reason]
        exemplar=items[0]
        entity("issues",reason,"I",{"check_name":"unresolved_attribution","description":
            f"{reason} Ячеек для сверки: {len(items)}.","source_page":str(exemplar["page"]),
            "source_fragment":exemplar["fragment"]})
    doc["records"] = confirmed
    doc["review_cells"] = unresolved
    return {"document":doc, "tables":tables}


def write_tables(out, articles):
    out = Path(out)
    out.mkdir(parents=True,exist_ok=True)
    for name,columns in SCHEMA["tables"].items():
        with (out/(name+".csv")).open("w",encoding="utf-8",newline="") as stream:
            writer=csv.DictWriter(stream,fieldnames=columns)
            writer.writeheader()
            for article in articles:
                writer.writerows(article["tables"][name])

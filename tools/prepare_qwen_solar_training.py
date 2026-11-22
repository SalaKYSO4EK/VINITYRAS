"""Make cell-selection examples from independently verified PDF cells and dev_gold."""
import argparse
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
import csv
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sys
import unicodedata

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"model_bundle"))
from article_analyzer.pdf_lab import table_records

METRICS=["PCE","Voc","Jsc","FF"]
HOLDOUT={"D013","D018","D019","D020","D021","D022"}
VALIDATION={"D010","D015","D016","D017"}


def token(value):
    return re.sub(r"[^\w]","",unicodedata.normalize("NFKC",value).lower())


def number(value):
    try:
        return Decimal(str(value).replace(",",".").replace("−","-"))
    except InvalidOperation:
        return None


def index(path):
    with path.open(encoding="utf-8-sig",newline="") as stream:
        return list(csv.DictReader(stream))


def gold_keys(path):
    labels={(d["publication_id"],d["device_id"]):d["device_label"] for d in index(path/"devices.csv")}
    groups=defaultdict(set)
    for r in index(path/"results.csv"):
        pid=r["publication_id"]
        label=labels.get((pid,r["device_id"]))
        norm=number(r["normalized_value"] or r["metric_value"])
        unit=r["normalized_unit"] or r["metric_unit"]
        if (label and norm is not None and unit and r["source_page"] and r["source_table"]
                and r["result_origin"]=="primary" and r["extraction_status"]=="extracted"):
            groups[pid].add((int(r["source_page"]),token(r["source_table"]),token(label),
                             r["metric_name"],norm,unit))
    return groups


def load_job(job):
    path,expected=job
    try:
        doc=table_records(path,METRICS)
    except Exception as err:
        print(path.stem,type(err).__name__,str(err),flush=True)
        return []
    groups=defaultdict(list)
    for row in doc["records"]:
        if row.get("ambiguity") or not row["normalized"]["value"]:
            continue
        metric, page, table, sample=row["metric"],row["page"],row["table"],row["sample"]
        cell=row["sources"]["value"]
        header=row["sources"]["header"]
        if not sample or not cell.get("bbox") or not header.get("text"):
            continue
        key=(page,token(table),token(sample),metric,number(row["normalized"]["value"]),row["normalized"]["unit"])
        groups[page,table,metric].append((row,key in expected))
    examples=[]
    for (page,table,metric), data in sorted(groups.items()):
        if len(data)<2:
            continue
        positions=sorted({round(cell["sources"]["value"]["bbox"][0],0) for cell,_ in data})
        per_label=defaultdict(list)
        catalog=[]
        for i,(row,verified) in enumerate(data[:15],1):
            cell=row["sources"]["value"]
            header=row["sources"]["header"]["text"]
            x=round(cell["bbox"][0],0)
            ident=f"C{i+2}_{positions.index(x)+1}"
            stat=row["context"].get("aggregation",{}).get("value","")
            catalog.append([ident,row["sample"],header,stat,cell["text"]])
            per_label[token(row["sample"])].append((ident,verified,row["sample"],cell["bbox"]))
        for possible in per_label.values():
            if not all(verified for _,verified,_,_ in possible):
                continue
            names=sorted({name for _,_,name,_ in possible})
            if len(names)!=1:
                continue
            query=f"Извлеки все значения {metric} для образца {names[0]} из {table} на физической странице {page}."
            examples.append({"publication":path.stem,"page":page,"table":table,"metric":metric,
                             "pdf_sha256":doc["sha256"],"criteria":query,"cells":catalog,
                             "ids":[i for i,_,_,_ in possible],"source_boxes":[box for _,_,_,box in possible]})
    # Do not fill a split with many questions from a large single article.
    return examples[:6]


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("corpus",type=Path)
    cli.add_argument("output",type=Path)
    args=cli.parse_args()
    gold=gold_keys(args.corpus/"dev_gold")
    paths=sorted((args.corpus/"dev").glob("*.pdf"))
    result={"train":[],"validation":[],"holdout":[]}
    with ProcessPoolExecutor(max_workers=4) as pool:
        for path, examples in zip(paths,pool.map(load_job,((p,gold[p.stem]) for p in paths))):
            group="holdout" if path.stem in HOLDOUT else "validation" if path.stem in VALIDATION else "train"
            result[group].extend(examples)
            print(path.stem,group,len(examples),flush=True)
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),"utf-8")
    print({group:len(items) for group,items in result.items()},flush=True)


if __name__=="__main__":
    main()

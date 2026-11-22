"""Build the RAG participant CSVs from PDFs, checkpointing individual documents."""
import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import sys
import time
import zipfile

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE/"model_bundle"))
from article_analyzer.rag_profile import SCHEMA, project, write_tables


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def save(path,data):
    temp=path.with_suffix(".tmp")
    temp.write_text(json.dumps(data,ensure_ascii=False,separators=(",",":")),"utf-8")
    temp.replace(path)


def extract_one(path, ocr_dir, cache_dir, version):
    started=time.perf_counter()
    digest=sha(path)
    ocr_file=ocr_dir/(path.stem+".json") if ocr_dir else None
    identity=[version,digest,path.name,sha(ocr_file) if ocr_file and ocr_file.is_file() else None]
    cache=cache_dir/(path.stem+".json")
    if cache.is_file():
        cached=json.loads(cache.read_bytes())
        if cached["identity"]==identity:
            return path.name,len(cached["data"]["tables"]["results"]),True
    data=project(path,ocr_dir)
    data["document"]["seconds"]=round(time.perf_counter()-started,3)
    save(cache,{"identity":identity,"data":data})
    return path.name,len(data["tables"]["results"]),False


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir",required=True,type=Path)
    parser.add_argument("--out",required=True,type=Path)
    parser.add_argument("--cache",required=True,type=Path)
    parser.add_argument("--ocr-dir",type=Path)
    parser.add_argument("--titles",type=Path)
    parser.add_argument("--ids",nargs="+")
    parser.add_argument("--workers",type=int,default=2)
    args=parser.parse_args()
    files=sorted(args.pdf_dir.glob("*.pdf"))
    if args.ids:
        files=[p for p in files if p.stem in args.ids]
        if {p.stem for p in files} != set(args.ids):
            parser.error("Missing input PDF for requested IDs")
    if not files or args.workers<1:
        parser.error("Expected PDFs and at least one worker")
    args.cache.mkdir(parents=True,exist_ok=True)
    sources=[Path(__file__),*(BASE/"model_bundle/article_analyzer"/name for name in
                 ("rag_documents.py","rag_profile.py","rag_tables.py","visible_pdf.py")),BASE/"model_bundle/config/rag-schema.json"]
    version=hashlib.sha256("".join(sha(p) for p in sources).encode()).hexdigest()
    print("Extractor",version, "PDFs",len(files),flush=True)
    start=time.perf_counter()
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        futures=[pool.submit(extract_one,p,args.ocr_dir,args.cache,version) for p in files]
        for job in as_completed(futures):
            print(*job.result(),flush=True)
    data=[json.loads((args.cache/(p.stem+".json")).read_bytes())["data"] for p in files]
    titles=json.loads(args.titles.read_bytes()) if args.titles else {}
    for entry in data:
        doc=entry["document"]
        title=titles.get(Path(doc["file"]).stem,{})
        if title.get("sha256")==doc["sha256"] and title.get("title") and title.get("source_fragment"):
            entry["tables"]["publications"][0]["title"]=title["title"]
            doc["bib"]["title"]=title["title"]
            doc["bib_sources"]["title"]={k:title[k] for k in ("title","source_fragment","source_page","status") if k in title}
            doc["bib_sources"]["title"]["method"]="local_nuextract_lora_verified"
    write_tables(args.out,data)
    ev=args.out/"evidence"
    ev.mkdir(exist_ok=True)
    for paper in data:
        doc=paper["document"]
        save(ev/(Path(doc["file"]).stem+".json"),doc)
    summary={"extractor":version,"input_directory":str(args.pdf_dir.resolve()),"documents":len(data),
             "tables":{t:sum(len(p["tables"][t]) for p in data) for t in SCHEMA["tables"]},
             "seconds_including_cache":round(time.perf_counter()-start,3),"articles":[{
                 "file":d["file"],"sha256":d["sha256"],"pages":d["pages"],"native_text_pages":d["text_pages"],
                 "ocr_pages":d["ocr_pages"],"table_cells":len(d["records"]),"review_cells":len(d["review_cells"]),
                 "scope_decision":p["tables"]["publications"][0]["in_scope"],"errors":d["errors"],
                 "seconds":d["seconds"]} for p in data for d in [p["document"]]]}
    save(args.out/"summary.json",summary)
    with zipfile.ZipFile(args.out/"submission.zip","w",compression=zipfile.ZIP_DEFLATED) as z:
        for name in SCHEMA["tables"]:
            z.write(args.out/(name+".csv"),name+".csv")
    print(json.dumps(summary["tables"]),flush=True)


if __name__=="__main__":
    main()

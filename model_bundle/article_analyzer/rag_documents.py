"""Bibliography and explicit statements from the supplied RAG paper."""
from pathlib import Path
import re
from statistics import median
import unicodedata

import pypdfium2 as pdfium


def flat(value):
    return " ".join(unicodedata.normalize("NFKC", value or "").split())


def compact(value):
    return re.sub(r"\s+", "", flat(value)).casefold()


def read_pages(path):
    pages = []
    with pdfium.PdfDocument(path) as pdf:
        metadata = pdf.get_metadata_dict()
        for page in pdf:
            try:
                text = page.get_textpage()
                try:
                    pages.append(text.get_text_bounded().replace("\r\n", "\n"))
                finally:
                    text.close()
            finally:
                page.close()
    return metadata, pages


def first_heading(page, first_text):
    chars = [c for c in page.chars if c["text"].strip() and 0 < c["top"] < page.height * .5]
    if len(chars) < 15:
        return None
    sizes = {}
    for c in chars:
        sizes.setdefault(round(c["size"], 1), []).append(c)
    sizes = sorted((s for s,g in sizes.items() if len(g) >= 16), reverse=True)
    if not sizes or sizes[0] < median(c["size"] for c in chars) * 1.16:
        return None
    chosen = [c for c in chars if abs(c["size"]-sizes[0]) < .6]
    top, bottom = min(c["top"] for c in chosen), max(c["bottom"] for c in chosen)
    if bottom-top > 7*sizes[0]:
        return None
    box = (min(c["x0"] for c in chosen),top,max(c["x1"] for c in chosen),bottom)
    fragment = page.crop(box).extract_text(x_tolerance=2) or ""
    title = flat(fragment)
    if not 4 <= len(title.split()) <= 55 or compact(title) not in compact(first_text):
        return None
    if re.search(r"(?:https?://|@|abstract\b|\buniversity\b|\bproceedings of\b)", title, re.I):
        return None
    return {"value":title, "fragment":fragment, "bbox":list(box), "page":1, "method":"printed_heading"}


def bibliography(metadata, page, first_text):
    first = flat(first_text)
    bib, sources = {}, {}
    title = flat(metadata.get("Title", ""))
    if len(title.split()) >= 3 and compact(title) in compact(first):
        sources["title"] = {"value":title, "page":1, "method":"printed_metadata"}
    elif page is not None:
        found = first_heading(page, first_text)
        if found:
            sources["title"] = found
    author = flat(metadata.get("Author", ""))
    names = [flat(name) for name in author.split(";") if flat(name)]
    if names and all(len(n.split()) >= 2 and compact(n) in compact(first) for n in names) and "," not in author:
        sources["authors"] = {"value":"; ".join(names), "page":1, "method":"printed_metadata"}
    doi = re.search(r"(?:doi\s*:\s*|doi\.org/)(10\.\d{4,9}/[^\s<>]+)", first[:3000], re.I)
    if doi:
        sources["DOI"] = {"value":doi[1].rstrip(".;,)]"), "page":1, "fragment":doi[0]}
    years = re.findall(r"©\s*((?:19|20)\d{2})\b|\b(?:ACL|EMNLP|NAACL|COLING|ICLR|SIGIR|NeurIPS)\s+((?:19|20)\d{2})\b",first[:3500], re.I)
    values = {a or b for a,b in years}
    if len(values) == 1:
        sources["year"] = {"value":values.pop(), "page":1, "method":"printed_venue_or_copyright"}
    for key, source in sources.items():
        bib[key] = source["value"]
    return bib, sources


RAG = re.compile(r"\bRAG\b|retrieval[\s\-–]+augmented|retriev(?:ed|al) (?:passages|documents|context)", re.I)
EVAL = re.compile(r"\b(?:experiments?|results?|benchmarks?|evaluat(?:e|es|ed|ion)|performance|outperform\w*)\b", re.I)
TASK = re.compile(r"\b(?:(?:open[ -]domain|multi[ -]hop|knowledge[ -]intensive|long[ -]form|factoid|conversational)\s+)*(?:question answering|fact (?:verification|checking)|text summarization|response generation|document retrieval|passage retrieval)\b", re.I)
TOP_K = re.compile(r"\b(?:we (?:first )?(?:retrieve|select|return) (?:the )?top[- ](\d{1,3}) (?:documents?|chunks?|passages?)|top[- ]?k\s*(?:=|is set to|was set to)\s*(\d{1,3})(?![\d.,]\d))\b",re.I)


def passages(pages):
    for number, text in enumerate(pages, 1):
        # Exclude bibliographies. The source PDF still provides independent tables in appendices.
        before = re.split(r"(?im)^\s*(?:\d+\s+)?(?:References|Bibliography)\s*$", text, maxsplit=1)
        for sentence in re.split(r"(?<=[.!?])\s+",flat(before[0])):
            if 18 <= len(sentence) <= 1000:
                yield number,sentence
        if len(before)>1:
            return


def scope_evidence(pages):
    corpus = [(i,flat(t)) for i,t in enumerate(pages[:4],1) if t.strip()]
    hits = {}
    for key,pattern in (("retrieval",RAG),("evaluation",EVAL)):
        for page,text in corpus:
            match = pattern.search(text)
            if match:
                hits[key] = {"page":page, "fragment":text[max(0,match.start()-45):match.end()+80]}
                break
    return hits

"""Cache a completed local per-device batch within a backend-verified runtime fingerprint."""
import hashlib
import json
import os
from pathlib import Path

from article_analyzer.criteria_cache import read_reply, save_reply


def batch_path(requests, documents, fields, pages_dir, threads):
    folder = os.environ.get("NEXUS_DEVICE_CACHE")
    if not folder:
        return None
    hashes = {doc["file"]: doc.get("sha256") for doc in documents}
    batch = []
    try:
        for request in requests:
            name = Path(request["path"]).name
            digest = hashes[name]
            if not isinstance(digest, str) or len(bytes.fromhex(digest)) != 32:
                return None
            try:
                with (pages_dir / (name + ".json")).open("rb") as stream:
                    page_digest = hashlib.file_digest(stream, "sha256").hexdigest()
            except FileNotFoundError:
                page_digest = None  # Worker extracts page text directly from this PDF.
            batch.append([name, digest, request["publication_id"], request["samples"], page_digest])
    except (OSError, ValueError, KeyError):
        return None
    key = json.dumps([1, threads, fields, batch], sort_keys=True, ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    return Path(folder) / (hashlib.sha256(key).hexdigest() + ".json")


def complete(report, requests):
    if not isinstance(report, dict):
        return False
    publications = report.get("publications")
    if not isinstance(publications, dict) or set(publications) != {r["publication_id"] for r in requests}:
        return False
    for request in requests:
        entries = publications[request["publication_id"]]
        if not isinstance(entries, list) or len(entries) != len(request["samples"]):
            return False
        for entry, sample in zip(entries, request["samples"]):
            if (not isinstance(entry, dict) or entry.get("status") not in ("source_supported", "no_supported_properties")
                    or any(entry.get(key) != value for key, value in sample.items())
                    or not all(isinstance(entry.get(key), dict) for key in ("fields", "unconfirmed", "prediction"))
                    or not isinstance(entry.get("seconds"), (int, float))
                    or not isinstance(entry.get("input"), str) or not entry["input"]
                    or not isinstance(entry.get("raw"), str) or not entry["raw"]):
                return False
    return True


def store(path, report, requests):
    if path and complete(report, requests):
        # read_reply/save_reply use a SHA-256 envelope and an atomic same-directory replace.
        save_reply(path, json.dumps({"key": path.stem, "report": report}, ensure_ascii=False))


def restore(path, requests):
    raw = read_reply(path)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        report = payload["report"]
        if payload["key"] != path.stem or not complete(report, requests):
            return None
        report["original_load_seconds"] = report["load_seconds"]
        report["load_seconds"] = 0
        report["cache"] = {"status": "hit", "key": path.stem}
        for entries in report["publications"].values():
            for entry in entries:
                entry["original_seconds"] = entry["seconds"]
                entry["seconds"] = 0
        return report
    except (TypeError, ValueError, KeyError):
        return None

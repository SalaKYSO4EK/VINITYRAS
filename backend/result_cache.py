"""Reuse the evidence and outputs of an identical, completed local analysis."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile

from uploads import valid_filename
from reference_check import REPORT_FILE


def file_hash(path):
    checksum = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def digest(items):
    return hashlib.sha256(json.dumps(items, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class ResultCache:
    def __init__(self, jobs, runner, python):
        self.jobs = jobs
        self.runner = runner
        self.python = python
        self.index = jobs / ".analysis-cache"
        self.digests = {}

    def engine_key(self, uses_qwen=True):
        """Fingerprint installed scientific code, templates, weights and interpreter configuration."""
        bundle = self.runner.parent
        if (bundle / "config/dual-qwen.json").is_file() and os.getenv("NEXUS_LEGACY_INFERENCE") != "1":
            return None
        if (os.getenv("NEXUS_DISABLE_CACHE") == "1" or self.runner.name != "run_inference.py"
                or not (bundle / "article_analyzer").is_dir()):
            return None
        settings = {k: os.getenv(k, "") for k in ("NEXUS_QWEN_API_BASE", "NEXUS_QWEN_API_MODEL",
                    "NEXUS_QWEN_API_TIMEOUT", "NEXUS_QWEN_GGUF", "NEXUS_QWEN_MODEL_REVISION")}
        remote = (bundle / "config/qwen-api.json").exists() or any(
            settings[k].strip() for k in ("NEXUS_QWEN_API_BASE", "NEXUS_QWEN_API_MODEL", "NEXUS_QWEN_API_TIMEOUT"))
        # An API alias alone cannot identify changes to weights on another computer.
        if uses_qwen and remote and not settings["NEXUS_QWEN_MODEL_REVISION"].strip():
            return None
        resources = [self.runner, self.python, Path(__file__).resolve(),
                     Path(__file__).with_name("pdf_criteria_runner.py")]
        for name in ("requirements.lock.txt", "manifest.json"):
            if (bundle / name).is_file():
                resources.append(bundle / name)
        for pattern in ("article_analyzer/*.py", "tools/*.py", "config/*.json", "submission_template/*.csv",
                        "models/NuExtract-1.5-tiny/*", "artifacts/**/*.json", "artifacts/**/*.safetensors"):
            resources.extend(p for p in bundle.glob(pattern) if p.is_file())
        if uses_qwen and not remote:
            weights = settings["NEXUS_QWEN_GGUF"] or "models/criteria/qwen2.5-1.5b-instruct-q4_k_m.gguf"
            resources.append(bundle / weights)
            resources.extend(p for p in (bundle / "models/criteria/llama-b8157").glob("*")
                             if p.suffix.lower() in (".exe", ".dll"))
        parts = []
        for path in sorted(resources):
            stat = path.stat()
            tag = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            previous = self.digests.get(path)
            if previous is None or previous[0] != tag:
                self.digests[path] = tag, file_hash(path)
            parts.append((str(path), self.digests[path][1]))
        return digest([parts, settings])

    def keys(self, directory):
        try:
            request = json.loads((directory / "request.json").read_text("utf-8"))
            criteria = request.get("criteria_pdf")
            engine = self.engine_key(bool(request["criteria"] or criteria))
            if engine is None:
                return None, None
            papers = [(item["name"], file_hash(directory / "input" / item["name"])) for item in request["files"]]
            attachment = ([criteria["name"], criteria.get("pages"), file_hash(directory / "criteria.pdf")]
                          if criteria else None)
            return engine, digest([engine, request["schema_version"], request["criteria"], attachment, papers])
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            logging.warning("Повторное использование временно недоступно.", exc_info=True)
            return None, None

    def device_store(self):
        try:
            engine = self.engine_key(uses_qwen=False)
            if engine is None:
                return None
            devices = self.index / "devices"
            store = devices / engine
            for path in (self.index, devices, store):
                path.mkdir(exist_ok=True)
                if path.is_symlink() or path.resolve() != path:
                    return None
            return store
        except (OSError, ValueError):
            logging.warning("Кэш локального извлечения свойств образцов временно недоступен.", exc_info=True)
            return None

    def remember(self, key, directory):
        if not key:
            return
        try:
            output = directory / "output"
            evidence = json.loads((output / "records.json").read_text("utf-8"))
            # Keep deterministic scientific limitations and their warnings. Retry failed model
            # workers and unreadable inputs rather than remembering a temporary failure.
            if (any(not str(issue.get("error", "")).startswith("ExtractionError:")
                    for issue in evidence.get("errors", []))
                    or any(not doc.get("sha256") for doc in evidence["documents"])):
                return
            if evidence.get("custom"):
                passes = (run for page in evidence["custom"]["pages"] for run in page["passes"].values())
                if any(run.get("error") or any(g.get("error") for g in run["groups"].values()) for run in passes):
                    return
            files = {p.name: file_hash(p) for p in output.iterdir()
                     if p.name != REPORT_FILE and p.is_file() and not p.is_symlink()}
            if not {"records.json", "criteria-plan.json", "qwen-raw.json"} <= files.keys():
                return
            self.index.mkdir(exist_ok=True)
            temporary = self.index / (directory.name + ".tmp")
            data = {"job": directory.name, "files": files, "checksum": digest([key, directory.name, files])}
            temporary.write_text(json.dumps(data), encoding="utf-8")
            temporary.replace(self.index / (key + ".json"))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            logging.warning("Не удалось сохранить индекс готового анализа.", exc_info=True)

    def restore(self, key, output):
        if not key:
            return False
        try:
            manifest = json.loads((self.index / (key + ".json")).read_text("utf-8"))
            job_id = manifest["job"]
            if not re.fullmatch(r"[0-9a-f]{32}", job_id):
                return False
            previous = self.jobs / job_id
            source = previous / "output"
            if (previous.is_symlink() or source.is_symlink()
                    or json.loads((previous / "status.json").read_text("utf-8"))["status"] != "completed"):
                return False
            files = manifest["files"]
            if (not isinstance(files, dict) or manifest["checksum"] != digest([key, job_id, files])
                    or not {"records.json", "report.json", "criteria-plan.json", "qwen-raw.json"} <= files.keys()):
                return False
            with tempfile.TemporaryDirectory(dir=output.parent, prefix="reuse-") as temporary:
                ready = Path(temporary) / "output"
                ready.mkdir()
                for name, expected in files.items():
                    if not valid_filename(name):
                        return False
                    path = source / name
                    if not path.is_file() or path.is_symlink():
                        return False
                    checksum = hashlib.sha256()
                    with path.open("rb") as reader, (ready / name).open("xb") as writer:
                        for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                            checksum.update(chunk)
                            writer.write(chunk)
                    if checksum.hexdigest() != expected:
                        return False
                ready.rename(output)
                return True
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False

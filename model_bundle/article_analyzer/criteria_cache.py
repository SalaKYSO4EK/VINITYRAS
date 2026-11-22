"""Cache parsed Qwen replies within one fingerprint of the installed model and prompts."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def entry_for(model, runtime, task, criteria, schema, max_tokens):
    folder = os.getenv("NEXUS_CRITERIA_CACHE")
    if not folder:
        return None
    request = json.dumps([model, runtime, task, criteria, schema, max_tokens], ensure_ascii=False)
    return Path(folder) / (hashlib.sha256(request.encode()).hexdigest() + ".json")


def read_reply(path):
    if path is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            raw = data["raw"]
            if isinstance(raw, str) and hashlib.sha256(raw.encode()).hexdigest() == data["sha256"]:
                return raw
        except (OSError, ValueError, TypeError, KeyError):
            pass
    return None


def save_reply(path, raw):
    if path is None:
        return
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(mode="w", dir=path.parent, delete=False, encoding="utf-8") as stream:
            temporary = Path(stream.name)
            json.dump({"raw": raw, "sha256": hashlib.sha256(raw.encode()).hexdigest()}, stream, ensure_ascii=False)
        temporary.replace(path)
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

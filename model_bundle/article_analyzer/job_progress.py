"""Publish current model-worker progress for a job started by the local API."""

import json
import os
from pathlib import Path
from threading import Lock
import time


_lock = Lock()


def _update(phase=None, **changes):
    with _lock:
        _write_update(phase, **changes)


def _write_update(phase=None, *, pdf_index=None, pdf_total=None, file=None, request_bytes=None):
    filename = os.environ.get("NEXUS_JOB_PROGRESS")
    if not filename:
        return
    path = Path(filename)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    try:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            state = {}
        if not isinstance(state, dict):
            state = {}
        current = time.time()
        state.setdefault("worker_started_at", current)
        state.setdefault("qwen_requests", 0)
        state.setdefault("qwen_request_bytes", 0)
        if phase is not None:
            phases = state.setdefault("phase_seconds", {})
            previous = state.get("phase")
            if previous != phase:
                if previous:
                    phases[previous] = round(phases.get(previous, 0) +
                                             max(0, current - state["phase_started_at"]), 3)
                state.update(phase=phase, phase_started_at=current)
            for key, value in (("pdf_index", pdf_index), ("pdf_total", pdf_total), ("file", file)):
                if value is None:
                    state.pop(key, None)
                else:
                    state[key] = value
        if request_bytes is not None:
            state["qwen_requests"] = state.get("qwen_requests", 0) + 1
            state["qwen_request_bytes"] = state.get("qwen_request_bytes", 0) + request_bytes
        temp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        for delay in (0.02, 0.06, None):
            try:
                temp.replace(path)
                break
            except PermissionError:
                if delay is None:
                    break
                time.sleep(delay)
    except OSError:
        # Status reporting should not stop an extraction when the job file is busy.
        pass
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def report_phase(phase, *, pdf_index=None, pdf_total=None, file=None):
    _update(phase, pdf_index=pdf_index, pdf_total=pdf_total, file=file)


def report_qwen_request(request_bytes):
    """Count UTF-8 bytes of the system and user text sent to Qwen."""
    _update(request_bytes=request_bytes)

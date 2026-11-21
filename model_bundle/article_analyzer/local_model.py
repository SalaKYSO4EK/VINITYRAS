"""Offline NuExtract inference boundary for the future PDF/CSV pipeline."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any


class ExtractionError(ValueError):
    def __init__(self, message: str, raw_output: str = "", *, truncated: bool = False):
        super().__init__(message)
        self.raw_output = raw_output
        self.truncated = truncated


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"Non-finite JSON constant: {value}")


def _shape(value: Any, template: Any, path: str = "$") -> None:
    if isinstance(template, dict):
        if not isinstance(value, dict) or set(value) != set(template):
            raise ValueError(f"Unexpected JSON fields at {path}")
        for key in template:
            _shape(value[key], template[key], f"{path}.{key}")
    elif isinstance(template, list):
        if not isinstance(value, list):
            raise ValueError(f"Expected JSON array at {path}")
        if len(template) > 1:
            raise ValueError("Array templates must contain zero or one prototype")
        for item in value:
            if template:
                _shape(item, template[0], f"{path}[]")
    elif isinstance(template, str):
        if not isinstance(value, str):
            raise ValueError(f"Expected extraction string at {path}")
    else:
        raise ValueError(f"Unsupported template type at {path}")


@dataclass(frozen=True)
class Candidate:
    data: dict
    raw_output: str
    input_tokens: int
    output_tokens: int
    seconds: float
    model_revision: str
    source_verified: bool = False


class LocalNuExtract:
    """Load only local weights; deterministic JSON generation without truncation.

    JSON shape validation is deliberately separate from scientific validation.
    No value returned by this class is marked as source-verified.
    """

    def __init__(self, model_path: str | Path, *, adapter_path: str | Path | None = None,
                 threads: int = 4):
        if threads < 1:
            raise ValueError("threads must be positive")
        os.environ["HF_HUB_OFFLINE"] = "1"
        os.environ["TRANSFORMERS_OFFLINE"] = "1"
        os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
        os.environ["DO_NOT_TRACK"] = "1"
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        path = Path(model_path).resolve(strict=True)
        manifest = json.loads((path / "local-manifest.json").read_text(encoding="utf-8"))
        if manifest.get("repo_id") != "numind/NuExtract-1.5-tiny":
            raise ValueError("Expected numind/NuExtract-1.5-tiny manifest")
        for item in manifest["files"]:
            file = (path / item["name"]).resolve(strict=True)
            if not file.is_relative_to(path):
                raise ValueError("Model manifest file is outside the model directory")
            with file.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != item["sha256"]:
                    raise ValueError(f"Model checksum mismatch: {item['name']}")
        self.revision = manifest["revision"]
        torch.set_num_threads(threads)
        started = time.perf_counter()
        self.tokenizer = AutoTokenizer.from_pretrained(path, local_files_only=True, trust_remote_code=False)
        self.model = AutoModelForCausalLM.from_pretrained(
            path, local_files_only=True, trust_remote_code=False,
            dtype=torch.float32, attn_implementation="eager",
        ).to("cpu")
        if adapter_path is not None:
            from peft import PeftModel
            adapter = Path(adapter_path).resolve(strict=True)
            if not all((adapter / name).is_file() for name in ("adapter_config.json", "adapter_model.safetensors")):
                raise ValueError("Local LoRA configuration and safetensors weights are required")
            self.model = PeftModel.from_pretrained(self.model, str(adapter), local_files_only=True)
        self.model.eval()
        self.load_seconds = time.perf_counter() - started

    @staticmethod
    def prompt(text: str, template: dict) -> str:
        if not isinstance(template, dict) or not template:
            raise ValueError("A non-empty extraction template is required")
        _shape(template, template)
        return (f"<|input|>\n### Template:\n{json.dumps(template, indent=4, ensure_ascii=False)}\n"
                f"### Text:\n{text}\n\n<|output|>")

    def extract(self, text: str, template: dict, *, max_new_tokens: int = 512) -> Candidate:
        import torch
        if not text.strip():
            raise ValueError("Source text is empty")
        if max_new_tokens < 1:
            raise ValueError("max_new_tokens must be positive")
        prompt = self.prompt(text, template)
        inputs = self.tokenizer(prompt, return_tensors="pt", add_special_tokens=False)
        count = inputs["input_ids"].shape[1]
        if count + max_new_tokens > self.model.config.max_position_embeddings:
            raise ValueError("Source chunk exceeds the model context; split it before extraction")
        started = time.perf_counter()
        self.model.eval()
        with torch.inference_mode():
            result = self.model.generate(**inputs, do_sample=False, max_new_tokens=max_new_tokens,
                                         pad_token_id=self.tokenizer.eos_token_id)
        output_ids = result[0, count:]
        raw = self.tokenizer.decode(output_ids, skip_special_tokens=True)
        if len(output_ids) == max_new_tokens and output_ids[-1].item() != self.tokenizer.eos_token_id:
            raise ExtractionError("Model output reached the token limit", raw, truncated=True)
        try:
            data = json.loads(raw.strip(), object_pairs_hook=_unique_object,
                              parse_constant=_reject_constant)
            _shape(data, template)
        except ValueError as exc:
            raise ExtractionError(f"Invalid model JSON: {exc}", raw) from exc
        return Candidate(data=data, raw_output=raw, input_tokens=count,
                         output_tokens=len(output_ids), seconds=time.perf_counter() - started,
                         model_revision=self.revision)

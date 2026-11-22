import copy
import hashlib
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "model_bundle"))
from article_analyzer import pdf_lab as lab
from article_analyzer.pdf_lab_devices import TEMPLATE


class DeviceStageCacheTest(TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.cache = self.root / "cache"
        self.enterContext(patch.dict(os.environ, {"NEXUS_DEVICE_CACHE": str(self.cache)}))
        self.fields = ["device_type"]
        self.threads = 4
        self.pdf = self.root / "A.pdf"
        self.pdf.write_bytes(b"test fixture for the isolated model boundary")
        self.sample = {"page": 1, "table": "Table 1", "sample": "S",
                       "source": {"text": "S", "bbox": [1, 2, 3, 4]}}
        self.doc = {"publication_id": "A", "file": self.pdf.name,
                    "device_inventory": {"known": [self.sample]},
                    "sha256": hashlib.sha256(self.pdf.read_bytes()).hexdigest()}
        self.pages = self.root / "pages"
        self.pages.mkdir()
        self.pages_file = self.pages / "A.pdf.json"
        self.pages_file.write_text('["solar cell"]')
        self.out = self.root / "out"
        self.out.mkdir()
        self.failure = False
        self.worker = self.enterContext(patch.object(lab, "launch_model", side_effect=self.worker_response))

    def worker_response(self, module, log, args, timeout, errors, **kwargs):
        request, output, _ = args
        data = json.loads(request.read_text(encoding="utf-8"))
        doc = data["documents"][0]
        if "titles" in data:
            Path(data["titles"]["finished"]).touch()
        prediction = dict(TEMPLATE, device_type="cell")
        entries = [dict(sample, status="source_supported", input="solar cell", prediction=prediction,
                        raw=json.dumps(prediction), seconds=1.5, unconfirmed={},
                        fields={"device_type": {"value": "cell", "source": {"page": 1, "quote": "solar cell"}}})
                   for sample in doc["samples"]]
        if self.failure == "short":
            entries.pop()
        elif self.failure == "error":
            entries[0]["status"] = "invalid_response"
        elif self.failure == "wrong_source":
            entries[0]["source"] = {"text": "different", "bbox": [2, 2, 3, 4]}
        elif self.failure == "crash":
            errors.append({"phase": "pdf_lab_devices", "error": "worker crashed"})
        output.write_text(json.dumps({"base_revision": "v1", "adapter_hashes": {"weights": "v1"},
                                     "load_seconds": 2, "publications": {doc["publication_id"]: entries}}))

    def run_stage(self, titles=()):
        return lab.run_device_stage([self.pdf], [copy.deepcopy(self.doc)], self.fields, self.out,
                                    self.threads, [], self.pages, titles)

    def test_repeat_matches_original_evidence_and_keeps_current_times_separate(self):
        original = self.run_stage()["publications"]["A"][0]
        # Repeating in a different job directory must use the current request's file paths.
        other = self.root / "new"
        other.mkdir()
        self.pdf = other / self.pdf.name
        self.pdf.write_bytes((self.root / self.pdf.name).read_bytes())
        self.out = other
        hit = self.run_stage()
        self.assertEqual(self.worker.call_count, 1)
        row = hit["publications"]["A"][0]
        for key in ("input", "raw", "fields", "unconfirmed", "source", "prediction"):
            self.assertEqual(row[key], original[key], key)
        self.assertEqual((hit["load_seconds"], row["seconds"], row["original_seconds"]), (0, 0, 1.5))
        self.assertEqual(hit["cache"]["status"], "hit")
        self.assertIn("cache_hit", (other / "device-journal.jsonl").read_text(encoding="utf-8"))
        request = json.loads((other / "device-requests.json").read_text(encoding="utf-8"))
        self.assertTrue(Path(request["documents"][0]["path"]).samefile(self.pdf))

    def test_changes_to_any_scientific_input_invalidate(self):
        self.run_stage()
        self.pages_file.write_text('["another page"]')
        self.run_stage()
        self.pages_file.unlink()
        self.run_stage()
        self.doc["sha256"] = hashlib.sha256(b"other revision").hexdigest()
        self.run_stage()
        self.sample["source"]["bbox"][0] = 2
        self.run_stage()
        self.fields = ["architecture"]
        self.run_stage()
        self.threads += 1
        self.run_stage()
        self.pdf = self.pdf.with_name("renamed.pdf")
        self.doc["file"] = self.pdf.name
        self.run_stage()
        self.assertEqual(self.worker.call_count, 8)

    def test_errors_missing_rows_and_wrong_sample_source_do_not_seed_cache(self):
        for failure in ("short", "error", "wrong_source", "crash"):
            with self.subTest(failure=failure):
                self.failure = failure
                self.run_stage()
                self.assertEqual(list(self.cache.glob("*.json")), [])
        self.failure = False
        self.run_stage()
        self.run_stage()
        self.assertEqual(self.worker.call_count, 5)

    def test_corrupted_or_misplaced_entry_recomputes_and_repairs(self):
        self.run_stage()
        original, = self.cache.glob("*.json")
        envelope = json.loads(original.read_text(encoding="utf-8"))
        envelope["raw"] += " "
        original.write_text(json.dumps(envelope))
        self.run_stage()
        self.assertEqual(self.worker.call_count, 2)
        raw = original.read_bytes()
        self.fields = ["architecture"]
        self.run_stage()
        other, = (path for path in self.cache.glob("*.json") if path != original)
        other.write_bytes(raw)
        self.run_stage()
        self.run_stage()
        self.assertEqual(self.worker.call_count, 4)

    def test_pending_titles_always_use_worker(self):
        self.run_stage()
        self.run_stage(titles=[self.pdf])
        self.assertEqual(self.worker.call_count, 2)
        self.assertIn("titles", json.loads((self.out / "device-requests.json").read_text(encoding="utf-8")))

    def test_cache_io_failure_does_not_block_worker(self):
        self.cache.write_text("ordinary file")
        self.run_stage()
        self.run_stage()
        self.assertEqual(self.worker.call_count, 2)

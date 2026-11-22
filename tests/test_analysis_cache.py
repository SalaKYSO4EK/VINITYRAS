from pathlib import Path
import json
import os
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "model_bundle"))
from result_cache import ResultCache
from jobs import Jobs
from article_analyzer.criteria import CriteriaInterpreter, InterpretationError, Source


class CacheTest(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {key: "" for key in
                          ("NEXUS_DISABLE_CACHE", "NEXUS_QWEN_GGUF", "NEXUS_QWEN_API_BASE",
                           "NEXUS_QWEN_API_MODEL", "NEXUS_QWEN_API_TIMEOUT", "NEXUS_QWEN_MODEL_REVISION")}))
        runner = self.root / "run_inference.py"
        runner.write_text("# test runner")
        (self.root / "article_analyzer").mkdir()
        self.weights = self.root / "models/criteria/qwen2.5-1.5b-instruct-q4_k_m.gguf"
        self.weights.parent.mkdir(parents=True)
        self.weights.write_bytes(b"weights")
        self.cache = ResultCache(self.root, runner, Path(sys.executable))
        self.job = self.root / ("a" * 32)
        self.job.mkdir()
        (self.job / "input").mkdir()
        self.paper = self.job / "input/paper.pdf"
        self.paper.write_bytes(b"test input")
        self.data = {"schema_version": 1, "criteria": "test", "files": [{"name": self.paper.name}]}

    def key(self):
        (self.job / "request.json").write_text(json.dumps(self.data), encoding="utf-8")
        return self.cache.keys(self.job)[1]

    def test_input_and_inference_identity_invalidation(self):
        original = self.key()
        self.assertIsNotNone(original)
        self.assertEqual(original, self.key())
        self.data["job_id"] = "new job"
        self.assertEqual(original, self.key())
        self.data["criteria"] = "different question"
        self.assertNotEqual(original, self.key())
        self.data["criteria"] = "test"
        self.paper.write_bytes(b"new bytes with same PDF name")
        self.assertNotEqual(original, self.key())
        self.paper.write_bytes(b"test input")
        self.weights.write_bytes(b"another version of the weights")
        self.assertNotEqual(original, self.key())
        os.environ["NEXUS_QWEN_API_BASE"] = "http://localhost:9001/v1"
        self.assertIsNone(self.key())
        os.environ["NEXUS_QWEN_MODEL_REVISION"] = "first revision"
        remote = self.key()
        self.assertIsNotNone(remote)
        os.environ["NEXUS_QWEN_MODEL_REVISION"] = "second revision"
        self.assertNotEqual(remote, self.key())

    def test_pdf_criteria_pages_and_publication_name_change_key(self):
        (self.job / "criteria.pdf").write_bytes(b"criteria")
        self.data["criteria_pdf"] = {"name": "requirements.pdf", "pages": "1"}
        original = self.key()
        self.data["criteria_pdf"]["pages"] = "2"
        self.assertNotEqual(original, self.key())
        self.data["criteria_pdf"]["pages"] = "1"
        (self.job / "input/renamed.pdf").write_bytes(self.paper.read_bytes())
        self.data["files"] = [{"name": "renamed.pdf"}]
        self.assertNotEqual(original, self.key())
        renamed = self.key()
        (self.job / "criteria.pdf").write_bytes(b"changed criteria PDF")
        self.assertNotEqual(renamed, self.key())

    def test_complete_output_only_and_corruption_falls_back_without_partial_files(self):
        out = self.job / "output"
        out.mkdir()
        key = self.key()
        (out / "records.json").write_text('{"documents":[{"sha256":"example"}],"errors":[]}')
        for name in ("qwen-raw.json", "criteria-plan.json", "report.json"):
            (out / name).write_text("{}")
        (out / "results.csv").write_text("value\n123\n")
        (self.job / "status.json").write_text('{"status":"completed"}')
        self.cache.remember(key, self.job)
        target = self.root / "new-job"
        target.mkdir()
        self.assertTrue(self.cache.restore(key, target / "output"))
        restored = target / "output/results.csv"
        self.assertEqual(restored.read_bytes(), (out / "results.csv").read_bytes())
        restored.write_text("independent job")
        self.assertNotEqual(restored.read_bytes(), (out / "results.csv").read_bytes())
        (out / "results.csv").write_text("damaged artifact")
        self.assertFalse(self.cache.restore(key, target / "second"))
        self.assertFalse((target / "second").exists())
        self.assertEqual(list(target.glob("reuse-*")), [])
        (self.job / "status.json").write_text('{"status":"failed"}')
        self.assertFalse(self.cache.restore(key, target / "third"))

    def test_criteria_reply_only_reused_after_parsing_with_identical_request(self):
        raw = '{"groups":[{"name":"sample","row":"one sample","fields":["pce"]}]}'
        generator = Mock(side_effect=[("bad JSON", 0), (raw, 7), (raw, 7)])
        interpreter = CriteriaInterpreter("Qwen", "test", generator)
        source = Source("query.txt", "evidence", {1:"Extract PCE"})
        with patch.dict(os.environ, {"NEXUS_CRITERIA_CACHE":str(self.root / "compiled")}):
            with self.assertRaises(InterpretationError):
                interpreter._generate(source, 120, fields_only=True)
            first, _ = interpreter._generate(source, 120, fields_only=True)
            second, _ = interpreter._generate(source, 120, fields_only=True)
            self.assertEqual(first, second)
            self.assertTrue(interpreter.raw_responses[-1]["reused"])
            interpreter._generate(source, 115, fields_only=True)
            self.assertNotIn("reused", interpreter.raw_responses[-1])
        self.assertEqual(generator.call_count, 3)

    def test_unpinned_remote_criteria_stay_fresh_while_local_devices_can_be_reused(self):
        os.environ["NEXUS_QWEN_API_BASE"] = "http://localhost:9001/v1"
        os.environ["NEXUS_QWEN_MODEL_REVISION"] = ""
        raw = '{"groups":[{"name":"sample","row":"one sample","fields":["pce"]}]}'
        completion = Mock(return_value=(raw, 0))
        manager = Jobs(self.root / "web-jobs", self.cache.runner, Path(sys.executable), timeout=10)
        store = manager.cache.index / "devices" / manager.cache.engine_key(uses_qwen=False)
        device_hits = []

        def run(command, *, env, **options):
            self.assertNotIn("NEXUS_CRITERIA_CACHE", env)
            self.assertEqual(Path(env["NEXUS_DEVICE_CACHE"]), store)
            self.assertTrue(store.is_dir())
            with patch.dict(os.environ, env, clear=True):
                interpreter = CriteriaInterpreter("Qwen", "test", completion)
                interpreter._generate(Source("query.txt", "evidence", {1: "Extract PCE"}), 120, fields_only=True)
            fixture = store / "fixture.json"
            device_hits.append(fixture.exists())
            fixture.write_text("{}", encoding="utf-8")
            output = Path(command[command.index("--out") + 1])
            (output / "results.csv").write_text("value\n123\n", encoding="utf-8")
            (output / "records.json").write_text('{"documents":[{"sha256":"example"}]}', encoding="utf-8")
            for name in ("qwen-raw.json", "criteria-plan.json", "report.json"):
                (output / name).write_text("{}", encoding="utf-8")
            return SimpleNamespace(returncode=0, poll=lambda: 0)

        manager.start()
        self.addCleanup(manager.close)
        with (patch.dict(os.environ, {"NEXUS_CRITERIA_CACHE": str(self.root / "unexpected-criteria"),
                                      "NEXUS_DEVICE_CACHE": str(self.root / "unexpected-device")}),
              patch("jobs.subprocess.Popen", side_effect=run) as spawn):
            for _ in range(2):
                queued = manager.enqueue("Extract PCE", [("paper.pdf", b"%PDF-1.7")])
                manager.pending.join()
                state = manager.status(queued["job_id"])
                self.assertEqual(state["status"], "completed")
                self.assertFalse(state.get("reused", False))
        self.assertEqual(spawn.call_count, 2)
        self.assertEqual(completion.call_count, 2)
        self.assertEqual(device_hits, [False, True])
        self.assertEqual(list(manager.cache.index.glob("*.json")), [])

    def test_device_store_is_optional_for_disabled_cache_unavailable_fingerprint_and_test_runner(self):
        with patch.dict(os.environ, {"NEXUS_DISABLE_CACHE": "1"}):
            self.assertIsNone(self.cache.device_store())
        probe_cache = ResultCache(self.root, self.root / "fake_runner.py", Path(sys.executable))
        self.assertIsNone(probe_cache.device_store())
        with (patch.object(self.cache, "engine_key", side_effect=OSError("unavailable")),
              self.assertLogs("root", level="WARNING")):
            self.assertIsNone(self.cache.device_store())

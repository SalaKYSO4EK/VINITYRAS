"""Check stable share links, gated file access and compact selected-row CSV."""

import csv
from email.message import Message
from io import StringIO
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from row_actions import SELECTED_COLUMNS, TableQuery, indexed_rows, locate_row, page_view
from server import MAX_SELECTION_BODY, ApiError, Handler


JOB = "a" * 32
OTHER = "b" * 32
SAMPLE = 'Состав "А, Б"\nсерия 2'
CITATION = 'Авторы: "10,5",\nпри 20 °C.'


class RowActionsTest(unittest.TestCase):
    def setUp(self):
        folder = TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.directory = self.root / JOB
        self.output = self.directory / "output"
        self.output.mkdir(parents=True)
        self.files = {}
        self.result_path = self.table("results.csv", [
            {"publication_id": "P078", "result_id": "R001", "measurement_id": "M001",
             "metric_value": "10,5", "metric_unit": "%", "source_page": "7", "source_fragment": CITATION},
            {"publication_id": "P045", "result_id": "R001", "measurement_id": "M001",
             "metric_value": "2", "metric_unit": "%", "source_page": "3", "source_fragment": "2"},
            {"publication_id": "P078", "result_id": "R002", "measurement_id": "",
             "metric_value": "20", "metric_unit": "%", "source_page": "2", "source_fragment": "20"},
        ])
        self.table("devices.csv", [
            {"publication_id": "P078", "device_id": "D001", "device_label": SAMPLE},
            {"publication_id": "P045", "device_id": "D001", "device_label": "Другой образец"},
        ])
        self.table("measurements.csv", [
            {"publication_id": "P078", "measurement_id": "M001", "device_id": "D001"},
            {"publication_id": "P045", "measurement_id": "M001", "device_id": "D001"},
        ])
        self.result = {"files": list(self.files.values()), "warnings": []}
        (self.directory / "result.json").write_text(json.dumps(self.result), encoding="utf-8")
        self.state = {"status": "completed", "source_files": ["P078.pdf"]}

    def table(self, filename, rows):
        path = self.output / filename
        columns = list(rows[0])
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        self.files[filename] = {"name": filename, "format": "csv", "columns": columns, "row_count": len(rows)}
        return path

    def handler(self):
        handler = object.__new__(Handler)
        handler.server = SimpleNamespace(jobs=SimpleNamespace(root=self.root, status=lambda job: self.state if job == JOB else None))
        handler.headers = Message()
        handler.received = None

        def capture(code, payload):
            handler.received = (code, payload)

        handler._json = capture
        return handler

    def set_payload(self, handler, selected):
        payload = json.dumps({"rows": selected}, ensure_ascii=False).encode("utf-8")
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(len(payload))
        handler.body_remaining = len(payload)
        handler._read_body = lambda size: payload
        handler._headers = lambda status, length, mime, extra: setattr(handler, "download", (status, length, mime, extra))
        handler._copy_response = lambda stream: setattr(handler, "body", stream.read())

    def test_ids_survive_numeric_sort_filter_and_pages(self):
        raw = page_view(self.result_path, JOB, TableQuery(0, 3), 3)
        ordered = page_view(self.result_path, JOB, TableQuery(0, 2, "metric_value"), 3)
        self.assertEqual([row["metric_value"] for row in ordered["rows"]], ["2", "10,5"])
        self.assertEqual(ordered["row_ids"], [raw["row_ids"][1], raw["row_ids"][0]])
        filtered = page_view(self.result_path, JOB, TableQuery(1, 1, filter_text="p078", filter_column="publication_id"), 3)
        self.assertEqual(filtered["row_ids"], [raw["row_ids"][2]])
        self.assertEqual(filtered["total"], 2)
        found = locate_row(self.result_path, JOB, TableQuery(0, 2, "metric_value"), 3, raw["row_ids"][2])
        self.assertEqual((found["index"], found["offset"], found["total"]), (2, 2, 3))
        self.assertEqual(len({*raw["row_ids"]}), 3)

    def test_content_fallback_preserves_duplicate_rows_and_job_scope(self):
        record = {"dataset": "test", "top_k": "17", "source_page": "8"}
        table = self.table("custom.csv", [record, record])
        ids = [row_id for row_id, _ in indexed_rows(table, JOB)]
        self.assertNotEqual(ids[0], ids[1])
        self.assertEqual(ids, [row_id for row_id, _ in indexed_rows(table, JOB)])
        ids_other_job = [row_id for row_id, _ in indexed_rows(table, OTHER)]
        self.assertTrue(set(ids).isdisjoint(ids_other_job))

    def test_api_returns_ids_and_rejects_invalid_locate_and_column(self):
        handler = self.handler()
        handler._read_job(JOB, ["tables", "results.csv"], "offset=1&limit=1")
        (code, value) = handler.received
        self.assertEqual(code, 200)
        self.assertEqual(len(value["row_ids"]), len(value["rows"]))
        row_id = value["row_ids"][0]
        handler._read_job(JOB, ["tables", "results.csv", "locate"], f"row_id={row_id}&limit=1")
        self.assertEqual(handler.received[1]["offset"], 1)
        for query, expected in (
            ("row_id=not-a-token", "invalid_row"),
            ("row_id=" + "0" * 32, "row_not_found"),
            (f"row_id={row_id}&sort=missing_field", "invalid_sort"),
            (f"row_id={row_id}&filter_column=missing_field", "invalid_filter"),
        ):
            with self.subTest(query=query), self.assertRaises(ApiError) as caught:
                handler._read_job(JOB, ["tables", "results.csv", "locate"], query)
            self.assertEqual(caught.exception.code, expected)

    def test_selected_csv_preserves_special_characters_and_same_publication_join(self):
        ids = [row_id for row_id, _ in indexed_rows(self.result_path, JOB)]
        handler = self.handler()
        self.set_payload(handler, [{"table": "results.csv", "row_id": ids[1]},
                                   {"table": "results.csv", "row_id": ids[0]}])
        handler._read_job(JOB, ["selected"], "", select=True)
        self.assertEqual(handler.download[:3], (200, len(handler.body), "text/csv; charset=utf-8"))
        self.assertIn('attachment', handler.download[3]["Content-Disposition"])
        data = list(csv.reader(StringIO(handler.body.decode("utf-8"), newline="")))
        self.assertEqual(data, [list(SELECTED_COLUMNS), ["Другой образец", "2", "%", "3", "2"],
                                [SAMPLE, "10,5", "%", "7", CITATION]])

    def test_selected_exports_all_cross_table_choices_in_order(self):
        sample = {"publication_id": "P078", "source_sample": "Контроль", "source_page": "5"}
        custom = self.table("rag_settings.csv", [dict(sample, top_k=str(i)) for i in range(501)])
        ambiguous = self.table("ambiguous.csv", [dict(sample, top_k="3", vector_size="24")])
        self.result["files"] = list(self.files.values())
        selections = [{"table": custom.name, "row_id": row_id} for row_id, _ in indexed_rows(custom, JOB)]
        selections.insert(1, {"table": ambiguous.name, "row_id": next(indexed_rows(ambiguous, JOB))[0]})
        handler = self.handler()
        self.set_payload(handler, selections)
        handler._selected(self.output, self.result, JOB)
        data = list(csv.reader(StringIO(handler.body.decode("utf-8"), newline="")))
        self.assertEqual(len(data), 503)
        self.assertEqual(data[1], ["Контроль", "0", "", "5", ""])
        self.assertEqual(data[2], ["Контроль", "", "", "5", ""])
        self.assertEqual(data[3], ["Контроль", "1", "", "5", ""])
        self.assertEqual(data[-1], ["Контроль", "500", "", "5", ""])

    def test_selected_rejects_outside_file_stale_row_and_limits(self):
        for items, status in (
            ([{"table": "../private.csv", "row_id": "1" * 32}], 400),
            ([{"table": "unpublished.csv", "row_id": "1" * 32}], 404),
            ([{"table": "results.csv", "row_id": "1" * 32}], 404),
            ([{"table": "results.csv", "row_id": "1" * 32}] * 2, 400),
        ):
            handler = self.handler()
            self.set_payload(handler, items)
            with self.subTest(status=status), self.assertRaises(ApiError) as caught:
                handler._read_job(JOB, ["selected"], "", select=True)
            self.assertEqual(caught.exception.status, status)
        handler = self.handler()
        handler.headers["Content-Type"] = "application/json"
        handler.headers["Content-Length"] = str(MAX_SELECTION_BODY + 1)
        handler.body_remaining = MAX_SELECTION_BODY + 1
        with self.assertRaises(ApiError) as caught:
            handler._read_job(JOB, ["selected"], "", select=True)
        self.assertEqual(caught.exception.status, 413)

    def test_findings_uses_published_sources_only(self):
        source = self.directory / "input" / "P078.pdf"
        source.parent.mkdir()
        source.write_bytes(b"%PDF-")
        handler = self.handler()
        result = {"source_file": "P078.pdf", "pages": [], "warnings": []}
        with patch("server.findings_for_source", return_value=result) as findings:
            handler._read_job(JOB, ["findings"], urlencode({"file": source.name}))
            self.assertEqual(handler.received, (200, result))
            findings.assert_called_once_with(self.output, source.name)
            for name in ("../P078.pdf", "Other.pdf"):
                with self.assertRaises(ApiError) as caught:
                    handler._read_job(JOB, ["findings"], urlencode({"file": name}))
                self.assertEqual(caught.exception.status, 404)
            findings.assert_called_once()

    def test_unavailable_jobs_retain_the_existing_errors(self):
        handler = self.handler()
        with self.assertRaises(ApiError) as missing:
            handler._read_job(OTHER, ["tables", "results.csv", "locate"], "row_id=" + "c" * 32)
        self.assertEqual((missing.exception.status, missing.exception.code), (404, "job_not_found"))
        self.state["status"] = "running"
        with self.assertRaises(ApiError) as unfinished:
            handler._read_job(JOB, ["findings"], "file=P078.pdf")
        self.assertEqual((unfinished.exception.status, unfinished.exception.code), (409, "result_unavailable"))


if __name__ == "__main__":
    unittest.main()

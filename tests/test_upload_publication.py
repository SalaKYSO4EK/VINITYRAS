"""Exercise Windows rename retries and cleanup of rejected uploads."""

from pathlib import Path
from tempfile import TemporaryDirectory
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from jobs import Jobs
from uploads import InvalidUpload


def access_denied():
    error = PermissionError("test Windows file hold")
    error.winerror = 5
    return error


class UploadPublicationTest(unittest.TestCase):
    def setUp(self):
        area = TemporaryDirectory()
        self.addCleanup(area.cleanup)
        root = Path(area.name)
        self.jobs = Jobs(root / "jobs", root / "runner.py", Path(sys.executable), 10)
        self.pdf = [("Paper.pdf", b"%PDF-1.7\n")]

    def test_transient_file_hold_retries_the_original_job_once(self):
        native = Path.rename
        calls = []

        def blocked_once(source, destination):
            calls.append((source, destination))
            if len(calls) == 1:
                raise access_denied()
            return native(source, destination)

        with patch.object(Path, "rename", autospec=True, side_effect=blocked_once), patch("jobs.time.sleep") as delay:
            state = self.jobs.enqueue("test criteria", self.pdf)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        delay.assert_called_once_with(0.05)
        self.assertEqual(self.jobs.pending.qsize(), 1)
        self.assertEqual(set(self.jobs.root.iterdir()), {self.jobs.root / state["job_id"]})
        self.assertEqual(self.jobs.status(state["job_id"]), state)

    def test_persistent_access_denied_does_not_leave_job_or_stage(self):
        with (patch.object(Path, "rename", side_effect=access_denied()) as rename,
              patch("jobs.time.sleep") as delay, self.assertLogs(level="WARNING"),
              self.assertRaises(InvalidUpload) as caught):
            self.jobs.enqueue("test criteria", self.pdf)

        self.assertEqual(rename.call_count, 5)
        self.assertEqual(delay.call_count, 4)
        self.assertEqual(caught.exception.status, 503)
        self.assertIn("Повторите отправку", str(caught.exception))
        self.assertTrue(self.jobs.pending.empty())
        self.assertEqual(list(self.jobs.root.iterdir()), [])

    def test_other_storage_errors_do_not_repeat(self):
        with (patch.object(Path, "rename", side_effect=OSError("test full disk")) as rename,
              patch("jobs.time.sleep") as delay, self.assertRaises(OSError)):
            self.jobs.enqueue("test criteria", self.pdf)
        rename.assert_called_once()
        delay.assert_not_called()
        self.assertTrue(self.jobs.pending.empty())
        self.assertEqual(list(self.jobs.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

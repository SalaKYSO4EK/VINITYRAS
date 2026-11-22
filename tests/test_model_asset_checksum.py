from hashlib import sha256
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "model_bundle"))

from article_analyzer.local_model import model_file_matches_digest


class ModelAssetChecksumTest(unittest.TestCase):
    def test_only_text_line_endings_may_change(self):
        original = b"first\nsecond\n"
        expected = sha256(original).hexdigest()
        with TemporaryDirectory() as folder:
            text = Path(folder) / "config.json"
            weights = Path(folder) / "model.safetensors"
            for asset in (text, weights):
                asset.write_bytes(original)
                self.assertTrue(model_file_matches_digest(asset, expected))

            text.write_bytes(b"first\r\nsecond\r\n")
            self.assertTrue(model_file_matches_digest(text, expected))

            weights.write_bytes(text.read_bytes())
            self.assertFalse(model_file_matches_digest(weights, expected))

            text.write_bytes(b"first\r\nchanged\r\n")
            self.assertFalse(model_file_matches_digest(text, expected))


if __name__ == "__main__":
    unittest.main()

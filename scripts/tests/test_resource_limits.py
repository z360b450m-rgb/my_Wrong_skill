from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from resource_limits import (  # noqa: E402
    SpillableJsonRows,
    read_json_bounded,
    write_json_spooled,
)


class ResourceLimitTests(unittest.TestCase):
    def test_spillable_rows_roll_to_disk_and_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            with SpillableJsonRows(32, directory) as rows:
                rows.append({"value": "x" * 100})
                self.assertTrue(rows.spilled_to_disk)
                self.assertEqual(list(rows.iter_batches(1)), [[{"value": "x" * 100}]])

    def test_json_output_uses_spooled_atomic_write(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output.json"
            spilled = write_json_spooled(
                {"value": "x" * 100},
                output,
                threshold_bytes=32,
                spill_directory=directory,
            )
            self.assertTrue(spilled)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["value"], "x" * 100)

    def test_json_input_size_is_bounded_before_read(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "large.json"
            source.write_text('{"value":"' + "x" * 100 + '"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "maximum allowed size"):
                read_json_bounded(source, max_bytes=32)


if __name__ == "__main__":
    unittest.main()

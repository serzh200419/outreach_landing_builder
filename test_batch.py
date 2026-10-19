"""Durable CSV progress tests; real browser rendering is verified separately."""
import contextlib
import csv
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

import main
import screenshot


def png(width, height):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\0" + b"\0" * width * 3) * height)) + chunk(b"IEND", b""))


class FakeSession:
    def __init__(self, action=None):
        self.paths = []
        self.action = action

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def capture(self, html):
        self.paths.append(html)
        if self.action:
            return self.action(html)
        for name, size in (("desktop.png", (1440, 1000)), ("mobile.png", (390, 844))):
            (html.parent / name).write_bytes(png(*size))


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / "businesses.csv"

    def write(self, rows, fields=None):
        fields = fields or list(dict.fromkeys(key for row in rows for key in row))
        with self.path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def read(self):
        with self.path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            return reader.fieldnames, list(reader)

    def run_batch(self, session=None, **kwargs):
        session = session or FakeSession()
        with patch("screenshot.ScreenshotSession", return_value=session) as factory:
            with contextlib.redirect_stdout(io.StringIO()):
                result = main.process_csv(self.path, "output", base=self.base, **kwargs)
        return result, session, factory

    def test_absent_processed_and_distinct_output_folders(self):
        self.write([{"business_name": "Հայերեն", "place_id": value} for value in ("one", "two")])
        result, session, factory = self.run_batch()
        self.assertEqual(len(result["completed"]), 2)
        self.assertEqual(len(set(path.parent for path in session.paths)), 2)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(self.read()[0][-1], "processed")
        self.assertTrue(self.path.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertEqual([row["processed"] for row in self.read()[1]], ["TRUE", "TRUE"])
        for html in result["completed"]:
            self.assertEqual({p.name for p in html.parent.iterdir()}, {"index.html", "desktop.png", "mobile.png"})

    def test_case_insensitive_true_and_original_order_with_limit(self):
        self.write([{"business_name": "Name", "place_id": str(i), "processed": flag}
                    for i, flag in enumerate((" tRuE ", "false", "", "TRUE", "yes"))])
        result, session, _ = self.run_batch(max_previews=2)
        self.assertEqual(result["already_processed"], 2)
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(result["remaining"], 1)
        self.assertEqual([p.parent.name for p in session.paths], [main.output_name("Name", str(i)) for i in (1, 2)])
        self.assertEqual(self.read()[1][0]["processed"], " tRuE ")

    def test_preserve_all_original_cells_and_move_processed_last(self):
        original = {"processed": " false ", "place_id": " id with spaces ", "business_name": "  Լուսե  ",
                    "phone_number": "'+374 (00) 000-005", "extra": ' Unicode Հայերեն, "quoted"\nsecond line ',
                    "address": "  unchanged address  "}
        self.write([original])
        result, _, _ = self.run_batch()
        self.assertEqual(len(result["completed"]), 1)
        fields, rows = self.read()
        self.assertEqual(fields, [key for key in original if key != "processed"] + ["processed"])
        self.assertEqual({k: v for k, v in rows[0].items() if k != "processed"},
                         {k: v for k, v in original.items() if k != "processed"})

    def test_missing_or_wrong_screenshot_never_marks_true(self):
        for variant in ("missing", "wrong-size", "corrupt"):
            with self.subTest(variant=variant):
                self.write([{"business_name": "Name", "place_id": variant}])
                def partial(html):
                    (html.parent / "desktop.png").write_bytes(png(1440, 1000))
                    if variant != "missing":
                        (html.parent / "mobile.png").write_bytes(png(1, 1) if variant == "wrong-size" else b"invalid")
                result, _, _ = self.run_batch(FakeSession(partial))
                self.assertEqual(result["completed"], [])
                self.assertEqual(len(result["failed"]), 1)
                self.assertEqual(self.read()[1][0]["processed"], "")

    def test_html_failure_skips_capture_and_continues(self):
        self.write([{"business_name": "Name", "place_id": value} for value in ("bad", "good")])
        (self.base / "personalization.json").write_text('{"bad":{"accent_color":"javascript:bad"}}')
        result, session, _ = self.run_batch()
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(len(session.paths), 1)
        self.assertEqual(len(result["completed"]), 1)
        self.assertEqual([r["processed"] for r in self.read()[1]], ["", "TRUE"])

    def test_screenshot_failure_leaves_row_unprocessed_even_with_old_files(self):
        self.write([{"business_name": "Name", "place_id": "one"}])
        folder = self.base / "output" / main.output_name("Name", "one")
        folder.mkdir(parents=True)
        for name, size in (("desktop.png", (1440, 1000)), ("mobile.png", (390, 844))):
            (folder / name).write_bytes(png(*size))
        def fail(html):
            raise RuntimeError("Chromium unavailable")
        result, _, _ = self.run_batch(FakeSession(fail))
        self.assertEqual(result["completed"], [])
        self.assertEqual(self.read()[1][0]["processed"], "")

    def test_missing_dependency_never_marks_processed(self):
        self.write([{"business_name": "Name", "place_id": "one"}])
        with patch.dict("sys.modules", {"playwright.sync_api": None}), contextlib.redirect_stdout(io.StringIO()):
            result = main.process_csv(self.path, "output", base=self.base)
        self.assertEqual(result["completed"], [])
        self.assertIn("pip install playwright", result["failed"][0][1])
        self.assertEqual(self.read()[1][0]["processed"], "")

    def test_repair_partial_output_without_deleting_old_files(self):
        self.write([{"business_name": "Name", "place_id": "one"}])
        folder = self.base / "output" / main.output_name("Name", "one")
        folder.mkdir(parents=True)
        (folder / "index.html").write_text("partial")
        (folder / "desktop.png").write_bytes(b"partial")
        (folder / "fullpage.png").write_bytes(b"keep this old file")
        result, _, _ = self.run_batch()
        self.assertEqual(len(result["completed"]), 1)
        self.assertIn("<!doctype html>", (folder / "index.html").read_text(encoding="utf-8"))
        screenshot.validate_png(folder / "mobile.png", (390, 844))
        self.assertEqual((folder / "fullpage.png").read_bytes(), b"keep this old file")

    def test_atomic_csv_failure_preserves_previous_valid_file(self):
        self.write([{"business_name": "Name", "place_id": "one", "processed": ""}])
        previous = self.path.read_bytes()
        with patch("main.os.replace", side_effect=PermissionError("Excel lock")):
            with self.assertRaisesRegex(main.CSVSaveError, "Close Excel"):
                main.save_csv_atomic(self.path, *self.read())
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual({p.name for p in self.base.iterdir()}, {"businesses.csv"})

    def test_tracking_failure_stops_before_next_business(self):
        self.write([{"business_name": "Name", "place_id": value, "processed": ""} for value in ("one", "two")])
        previous = self.path.read_bytes()
        with patch("main.save_csv_atomic", side_effect=main.CSVSaveError("Close Excel and rerun")):
            result, session, _ = self.run_batch()
        self.assertEqual(len(session.paths), 1)
        self.assertEqual(result["attempted"], 1)
        self.assertIn("Close Excel", result["fatal"])
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(result["remaining"], 2)

    def test_completed_progress_survives_later_tracking_failure(self):
        self.write([{"business_name": "Name", "place_id": value, "processed": ""} for value in ("one", "two", "three")])
        real_save = main.save_csv_atomic
        calls = []
        def save(path, fields, rows):
            calls.append(path)
            if len(calls) == 2:
                raise main.CSVSaveError("Close Excel and rerun")
            real_save(path, fields, rows)
        with patch("main.save_csv_atomic", side_effect=save):
            result, _, _ = self.run_batch()
        self.assertEqual(result["attempted"], 2)
        self.assertEqual(len(result["completed"]), 1)
        self.assertEqual([r["processed"] for r in self.read()[1]], ["TRUE", "", ""])

    def test_duplicate_flags_consistent_and_conflicts_rejected_before_limit(self):
        self.write([
            {"business_name": "Same", "place_id": "same", "processed": "TRUE"},
            {"business_name": "First pending", "place_id": "first", "processed": ""},
            {"business_name": "Same", "place_id": "same", "processed": "false"},
            {"business_name": "Same", "place_id": "same", "processed": ""},
            {"business_name": "Conflict A", "place_id": "conflict", "processed": ""},
            {"business_name": "Conflict B", "place_id": "conflict", "processed": "TRUE"}])
        result, session, _ = self.run_batch()
        self.assertEqual(result["attempted"], 2)
        self.assertEqual([p.parent.name for p in session.paths], [main.output_name("First pending", "first"), main.output_name("Same", "same")])
        self.assertIn("Conflicting duplicate", result["failed"][0][1])
        self.assertEqual([r["processed"] for r in self.read()[1]], ["TRUE", "TRUE", "TRUE", "TRUE", "", "TRUE"])

    def test_required_fields_and_structural_validation(self):
        self.write([{"business_name": "", "place_id": "bad"}, {"business_name": "Good", "place_id": "good"}])
        result, _, _ = self.run_batch()
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(len(result["completed"]), 1)
        self.path.write_text('business_name,place_id\nName,one,extra\n', encoding="utf-8")
        previous = self.path.read_bytes()
        result, _, factory = self.run_batch()
        self.assertIn("wrong number", result["fatal"])
        self.assertFalse(factory.called)
        self.assertEqual(self.path.read_bytes(), previous)

    def test_single_instance_lock_released_after_failure(self):
        self.write([{"business_name": "Name", "place_id": "one"}])
        with main.csv_lock(self.path):
            result, _, factory = self.run_batch()
            self.assertIn("Another instance", result["fatal"])
            self.assertFalse(factory.called)
        result, _, _ = self.run_batch()
        self.assertEqual(len(result["completed"]), 1)


if __name__ == "__main__":
    unittest.main()

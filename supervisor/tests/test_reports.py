"""Reports from the tools the agent built: checked data, never a page."""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["UVMC_NO_SUDO"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from uvmc.reports import attach_images, image_type, parse  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32

FULL = {
    "title": "Site check", "status": {"state": "warn", "text": "2 pages changed"},
    "blocks": [
        {"type": "heading", "text": "Changes"},
        {"type": "text", "md": "Checked **14 pages**."},
        {"type": "stats", "items": [{"label": "Pages", "value": 14, "note": "+2", "trend": "up"}]},
        {"type": "progress", "label": "Disk", "value": 72, "max": 100},
        {"type": "table", "columns": ["Page", "Change"], "rows": [["/pricing", "price went up"], ["/about"]]},
        {"type": "list", "items": ["a", "b"], "numbered": True},
        {"type": "chart", "kind": "bar", "x": ["Mon", "Tue", "Wed"], "series": [{"name": "ms", "values": [210, "x", 190]}]},
        {"type": "diff", "before": "old\n", "after": "new\n"},
        {"type": "image", "file": "plot.png", "alt": "A plot"},
        {"type": "link", "text": "Pricing", "url": "https://example.com/pricing"},
        {"type": "code", "text": "raw"},
        {"type": "columns", "columns": [[{"type": "text", "md": "left"}], [{"type": "text", "md": "right"}]]},
        {"type": "section", "title": "Details", "blocks": [{"type": "code", "text": "more"}]},
    ],
}


class ParseTest(unittest.TestCase):
    def test_every_block(self):
        r, problems = parse(json.dumps(FULL))
        self.assertEqual(problems, [])
        kinds = [b["type"] for b in r["blocks"]]
        self.assertEqual(kinds, ["heading", "text", "stats", "progress", "table", "list", "chart", "diff",
                                 "image", "link", "code", "columns", "section"])
        self.assertEqual(r["status"], {"state": "warn", "text": "2 pages changed"})
        self.assertEqual(r["blocks"][2]["items"][0]["value"], "14")             # all text for the panel
        self.assertEqual(r["blocks"][4]["rows"][1], ["/about", ""])             # short rows are filled
        self.assertEqual(r["blocks"][6]["series"][0]["values"], [210, None, 190])  # not a number: a gap
        self.assertIn("-old", r["blocks"][7]["text"])
        self.assertIn("+new", r["blocks"][7]["text"])
        self.assertEqual(r["images"], ["plot.png"])

    def test_what_is_refused(self):
        bad = [
            ({"type": "link", "url": "javascript:alert(1)"}, "https://"),
            ({"type": "link", "url": "https://x.com/\"onmouseover=\"x"}, "https://"),
            ({"type": "image", "file": "../../etc/passwd"}, "inside the tool's HOME"),
            ({"type": "image", "file": "/etc/passwd"}, "inside the tool's HOME"),
            ({"type": "html", "html": "<script>"}, "unknown block type"),
            ({"type": "columns", "columns": [[{"type": "columns", "columns": [[], []]}], []]}, "can't be inside"),
            ({"type": "columns", "columns": [[]]}, "2 or 3"),
            ({"type": "chart", "x": []}, "chart needs"),
            ({"type": "progress", "value": 5, "max": 0}, "progress"),
        ]
        for block, words in bad:
            r, problems = parse(json.dumps({"title": "t", "blocks": [block, {"type": "text", "md": "kept"}]}))
            self.assertIn(words, " ".join(problems), block)
            self.assertEqual(r["blocks"][-1], {"type": "text", "md": "kept"})   # the rest is still shown

    def test_whole_report_problems(self):
        self.assertIsNone(parse(b"not json")[0])
        self.assertIsNone(parse(b"[1, 2]")[0])
        self.assertIsNone(parse(b"{}")[0])
        self.assertIsNone(parse(b"x" * 400_000)[0])
        r, problems = parse(json.dumps({"blocks": [{"type": "text", "md": "x"}] * 150}))
        self.assertEqual(len(r["blocks"]), 100)
        self.assertTrue(problems)
        _, problems = parse(json.dumps({"title": "t", "status": {"state": "great"}}))
        self.assertIn("status", problems[0])

    def test_a_report_can_ask_for_a_notification(self):
        self.assertEqual(parse(json.dumps({"title": "t", "notify": "Price went up"}))[0]["notify"], "Price went up")
        self.assertEqual(parse(json.dumps({"title": "Daily", "notify": True}))[0]["notify"], "Daily")
        self.assertEqual(parse(json.dumps({"title": "t"}))[0]["notify"], "")
        self.assertEqual(parse(json.dumps({"title": "t", "notify": False}))[0]["notify"], "")

    def test_text_stays_text(self):
        r, _ = parse(json.dumps({"title": "<script>x</script>", "blocks": [{"type": "text", "md": "<img onerror=x>"}]}))
        self.assertEqual(r["title"], "<script>x</script>")   # the panel escapes it; nothing is turned into markup here

    def test_images_are_checked_by_their_bytes(self):
        self.assertEqual(image_type(PNG), "image/png")
        self.assertIsNone(image_type(b"<svg onload=alert(1)>"))
        r, problems = parse(json.dumps({"blocks": [{"type": "image", "file": "a.png"}, {"type": "image", "file": "b.png"},
                                                   {"type": "image", "file": "c.png"}]}))
        files = {"a.png": PNG, "b.png": b"<svg/>"}
        with tempfile.TemporaryDirectory() as d:
            attach_images(r, files.get, d, problems)
            self.assertEqual(os.listdir(d), ["1"])
        self.assertEqual([b.get("n") for b in r["blocks"]], [1, None, None])
        self.assertEqual(r["images"], 1)
        self.assertEqual(len(problems), 2)   # b is no image, c is missing


if __name__ == "__main__":
    unittest.main()

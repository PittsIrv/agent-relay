import re
import unittest
import xml.dom.minidom
from xml.sax.saxutils import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL = (r"/Users/", r"/home/\w", r"/var/folders", r"/private/", r"/tmp/", r"[A-Za-z]:\\", r"@[a-z0-9-]+\.(com|edu|org|net)")


class DemoAssetTests(unittest.TestCase):
    def test_the_demo_svg_is_valid_animated_and_free_of_local_details(self):
        text = (ROOT / "docs" / "assets" / "relay-demo.svg").read_text()
        xml.dom.minidom.parseString(text)
        self.assertIn("@keyframes", text)
        self.assertIn("prefers-reduced-motion", text)
        self.assertIn("<desc", text)
        for pattern in LOCAL:
            self.assertIsNone(re.search(pattern, text), pattern)

    def test_the_transcript_matches_the_svg_and_the_readme_embeds_both(self):
        transcript = (ROOT / "docs" / "demo-transcript.txt").read_text()
        svg = (ROOT / "docs" / "assets" / "relay-demo.svg").read_text()
        readme = (ROOT / "README.md").read_text()
        self.assertIn("docs/assets/relay-demo.svg", readme)
        self.assertIn(transcript.strip(), readme)
        for pattern in LOCAL:
            self.assertIsNone(re.search(pattern, transcript), pattern)
        for line in transcript.splitlines():
            if line.strip() and "`" not in line:
                self.assertIn(escape(line), svg)


if __name__ == "__main__":
    unittest.main()

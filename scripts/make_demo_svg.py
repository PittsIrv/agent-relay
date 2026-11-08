#!/usr/bin/env python3
"""Regenerate the README's animated terminal demo from a real `relay demo` run.

Usage: python scripts/make_demo_svg.py        (writes docs/assets/relay-demo.svg and docs/demo-transcript.txt)

Needs nothing but this checkout. The demo is offline and uses fake agents and a simulated GitHub,
so the output contains no accounts. The script refuses to write if the output holds a local path.
Lines appear one after another in a loop; with reduced motion requested, all lines show at once.
"""

import re
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
FONT, LINE, PAD_X, TOP = 13, 19, 22, 52
CHAR = 7.85  # advance of the 13px monospace stack, close enough for the box width


def demo_lines():
    run = subprocess.run([sys.executable, "-m", "agent_relay", "demo"], cwd=ROOT, text=True, capture_output=True,
                         env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"})
    if run.returncode:
        sys.exit(f"relay demo failed: {run.stderr[-300:]}")
    lines = run.stdout.rstrip("\n").splitlines()
    return [line for line in lines if not line.startswith("{") and not line.startswith("No model calls")]


def check_clean(text):
    for pattern in (r"/Users/", r"/home/", r"/var/", r"/tmp/", r"/private/", r"[A-Za-z]:\\\\", r"@[a-z0-9-]+\.[a-z]{2,}"):
        if re.search(pattern, text):
            sys.exit(f"refusing to write: output matches {pattern!r}")


def style(index, count, total):
    start = 100 * (0.04 + 0.78 * index / max(count, 1))
    return (f"@keyframes l{index}{{0%,{start:.2f}%{{opacity:0}}{start + 0.4:.2f}%,93%{{opacity:1}}100%{{opacity:0}}}}"
            f".t{index}{{animation:l{index} {total}s linear infinite}}")


def colour(line):
    if line.startswith("$ "):
        return "cmd"
    if re.match(r"^\d\. ", line):
        return "step"
    if line.startswith("What collaborators see"):
        return "step"
    if "Issue #" in line or "PR #" in line:
        return "head"
    return "out"


def build(lines):
    shown = ["$ relay demo", "", *lines]
    total = round(4 + 0.55 * len(shown), 1)
    width = int(PAD_X * 2 + CHAR * max(len(line) for line in shown)) + 8
    height = TOP + LINE * len(shown) + 28
    css = "".join(style(i, len(shown), total) for i in range(len(shown)))
    texts = []
    for i, line in enumerate(shown):
        y = TOP + LINE * (i + 1) - 5
        texts.append(f'<text class="t{i} {colour(line)}" x="{PAD_X}" y="{y}" xml:space="preserve">{escape(line)}</text>')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-labelledby="t d">
<title id="t">Agent Relay demo</title>
<desc id="d">A terminal runs relay demo. Alice's session hits a usage limit, her work is saved and posted as a GitHub issue, Bob's agent continues it and opens a draft PR, and Alice merges it. The full text is below this image.</desc>
<style>
text{{font:{FONT}px/1 ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;fill:#d5dfe4;white-space:pre}}
.cmd{{fill:#7fd6d9}}.step{{fill:#f2c96d}}.head{{fill:#9ec5ff}}
{css}
@media (prefers-reduced-motion:reduce){{text{{animation:none!important;opacity:1!important}}}}
</style>
<rect width="{width}" height="{height}" rx="9" fill="#0f171c"/>
<rect width="{width}" height="32" rx="9" fill="#1b2831"/><rect y="20" width="{width}" height="12" fill="#1b2831"/>
<circle cx="20" cy="16" r="5" fill="#ff6b61"/><circle cx="38" cy="16" r="5" fill="#f2c96d"/><circle cx="56" cy="16" r="5" fill="#5fcf80"/>
{chr(10).join(texts)}
</svg>
'''


def main():
    lines = demo_lines()
    text = "\n".join(lines)
    check_clean(text)
    (ROOT / "docs" / "assets").mkdir(exist_ok=True)
    (ROOT / "docs" / "assets" / "relay-demo.svg").write_text(build(lines))
    (ROOT / "docs" / "demo-transcript.txt").write_text("$ relay demo\n" + text + "\n")
    print(f"wrote docs/assets/relay-demo.svg ({len(lines)} lines) and docs/demo-transcript.txt")


if __name__ == "__main__":
    main()

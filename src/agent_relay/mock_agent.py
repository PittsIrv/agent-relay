"""Offline demonstration fixture. Never used by a real provider adapter."""
import json
import sys
import time
from pathlib import Path

sys.stdin.read()
mode = sys.argv[1]
if mode == "limit":
    Path("answer.py").write_text("def answer():\n    # unfinished work from the first worker\n    return None\n")
    print(json.dumps({"type": "error", "error": {"code": "usage_limit_reached"}}))
    sys.exit(1)
if mode == "finish":
    assert "return None" in Path("answer.py").read_text(), "successor did not receive partial work"
    Path("answer.py").write_text("def answer():\n    return 42\n")
elif mode == "finish-fresh":
    Path("answer.py").write_text("def answer():\n    return 42\n")
elif mode == "secret":
    Path(".env").write_text("DEMO_SECRET=do-not-publish\n")
elif mode == "wait":
    Path("partial.txt").write_text("recover me\n")
    time.sleep(60)
elif mode == "error":
    print(json.dumps({"type": "error", "error": "authentication_failed"}))
    sys.exit(1)

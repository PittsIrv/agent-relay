import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

from . import config, interactive, tasks
from .conversation import Conversation
from .gitstore import Store, git, output
from .worker import Worker

CHECK = [sys.executable, "-c", "from answer import answer; assert answer() == 42"]


def setup(root, github=False):
    """Bare remote plus two independent clones. With github=True, a fake `gh` records the conversation."""
    root = Path(root)
    remote, a, b = [root / name for name in ("shared.git", "alice", "bob")]
    git(root, "init", "--bare", "--initial-branch=main", str(remote))
    git(root, "clone", "--quiet", str(remote), str(a))
    for key, value in (("user.name", "Relay Demo"), ("user.email", "demo@example.invalid")):
        git(a, "config", key, value)
    (a / "README.md").write_text("Offline relay fixture\n")
    git(a, "add", "README.md")
    git(a, "commit", "--quiet", "-m", "fixture")
    git(a, "push", "--quiet", "origin", "main")
    git(root, "clone", "--quiet", str(remote), str(b))
    for key, value in (("user.name", "Relay Demo"), ("user.email", "demo@example.invalid")):
        git(b, "config", key, value)
    fake = root / "github.json"
    if github:
        fake.write_text(json.dumps({"next": 1, "issues": {}, "comments": [], "pulls": {}, "labels": ["relay"],
                                    "permissions": {"alice": "admin", "bob": "write"}, "reactions": []}))
    for repo in (a, b):
        owner = repo.name
        extra = {}
        if github:
            extra = {"github": "demo/shared", "owner": owner, "default_branch": "main",
                     "gh": ["env", f"FAKE_GH_STATE={fake}", f"FAKE_GH_LOGIN={owner}",
                            sys.executable, str(Path(__file__).with_name("fake_gh.py"))]}
        cfg = config.initialize(repo, "origin", CHECK, **extra)
        cfg["mode"] = "pooled"
        config.save(repo, cfg)
    return a, b, output(a, "rev-parse", "HEAD")


def mock(repo, mode, **kwargs):
    kwargs.setdefault("agent", "simulated agent")
    return Worker(repo, argv=[sys.executable, str(Path(__file__).with_name("mock_agent.py")), mode],
                  poll=0.05, **kwargs)


def thread(root):
    """Render the fake GitHub issue and PR the way a collaborator would read them."""
    db = json.loads((Path(root) / "github.json").read_text())
    lines = []
    for number, issue in db["issues"].items():
        lines.append(f"  Issue #{number}: {issue['title']}")
        for comment in db["comments"]:
            if comment["issue_url"].endswith(f"/issues/{number}"):
                text = re.sub(r"<!--.*?-->", "", comment["body"]).strip()
                text = re.sub(r"\[(`[0-9a-f]+`)\]\([^)]*\)", r"\1", text)
                body = "\n".join(f"      {line}" if line else "" for line in text.splitlines())
                lines.append(f"    {comment['user']['login']} commented:\n{body}")
    for number, pull in db["pulls"].items():
        draft = "draft " if pull["draft"] else ""
        lines.append(f"  {draft.capitalize()}PR #{number}: {pull['title']}  ({pull['head']} -> {pull['base']})")
    return "\n".join(lines)


def run():
    quiet = lambda _: None
    with tempfile.TemporaryDirectory(prefix="agent-relay-demo-") as folder:
        a, b, base = setup(folder, github=True)
        store = Store(a)
        print("Two collaborators, two clones, one shared remote. GitHub is simulated offline.\n")

        print("1. Alice works interactively in Claude Code. Mid-task, it hits the five-hour usage limit.")
        (a / "answer.py").write_text("def answer():\n    # Alice started this; the session was cut off\n    return None\n")
        transcript = Path(folder) / "alice-session.jsonl"
        transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "Implement answer() so it returns 42"}}) + "\n")
        config.update(a, share_prompt=True)  # Alice opted in with `relay hook install --share-prompt`.
        hook = {"hook_event_name": "StopFailure", "error": "rate_limit", "cwd": str(a),
                "transcript_path": str(transcript)}
        task = interactive.run_hook(stdin=io.StringIO(json.dumps(hook)), emit=quiet)
        assert task and task["status"] == "queued" and task["checkpoint"]
        assert output(a, "status", "--porcelain") == "?? answer.py", "Alice's working tree must be untouched"
        print(f"   The StopFailure hook (no model call) saved her uncommitted edit as {task['checkpoint']['sha'][:7]}")
        print(f"   and opened issue #{task['issue']}. Alice's own branch and files are unchanged.\n")

        print("2. Bob's worker is online with spare quota. It claims the task, continues Alice's code,")
        print("   runs Bob's local check, and opens a draft PR.")
        mock(b, "finish", emit=quiet).run(once=True)
        task = store.read()[1]["tasks"][task["id"]]
        assert task["status"] == "review" and task["verification"]["result"] == "passed" and task["pr"]
        assert output(a, "ls-remote", "origin", "refs/heads/main").split()[0] == base
        print(f"   Check passed. Draft PR #{task['pr']} opened; main is unchanged.\n")

        print("3. Alice reviews and merges the PR. Any clone's sync marks the task done.")
        db_path = Path(folder) / "github.json"
        db = json.loads(db_path.read_text())
        db["pulls"][str(task["pr"])]["merged"] = True
        db_path.write_text(json.dumps(db))
        report = Conversation(a, config.load(a), store, quiet).sync("main")
        assert report["done"] == [task["id"]]
        print("\nWhat collaborators see on GitHub:\n")
        print(thread(folder))
        print("\n" + json.dumps({"result": "PASS", "attempts": 2, "check": "passed",
                                 "human_handoff_steps": 0, "main_unchanged_until_review": True}))
        print("No model calls, network access, or credentials used. Temporary fixture removed on exit.")

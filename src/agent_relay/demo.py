import json
import sys
import tempfile
from pathlib import Path

from . import config, tasks
from .gitstore import Store, git, output
from .worker import Worker


def setup(root):
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
    for repo in (a, b):
        cfg = config.initialize(repo, "origin", [sys.executable, "-c", "from answer import answer; assert answer() == 42"])
        cfg["mode"] = "pooled"
        config.save(repo, cfg)
    return a, b, output(a, "rev-parse", "HEAD")


def mock(repo, mode, **kwargs):
    return Worker(repo, argv=[sys.executable, str(Path(__file__).with_name("mock_agent.py")), mode],
                  poll=0.05, **kwargs)


def run():
    with tempfile.TemporaryDirectory(prefix="agent-relay-demo-") as folder:
        a, b, base = setup(folder)
        store = Store(a)
        task_id = tasks.add(store, "Implement answer() returning 42", base)
        print("1. Alice starts and encounters a simulated usage limit.")
        mock(a, "limit").run(once=True)
        _, state = store.read()
        task = state["tasks"][task_id]
        assert task["status"] == "queued" and task["checkpoint"]
        first = task["checkpoint"]["sha"]
        print("2. Bob continues Alice's unfinished code in an independent clone.")
        mock(b, "finish").run(once=True)
        _, state = store.read()
        task = state["tasks"][task_id]
        assert task["status"] == "review" and task["verification"]["result"] == "passed"
        assert task["checkpoint"]["sha"] != first
        assert output(a, "ls-remote", "origin", "refs/heads/main").split()[0] == base
        print(json.dumps({"result": "PASS", "attempts": task["epoch"], "check": "passed", "main_unchanged": True}))
        print("No model calls, GitHub writes, or credentials used. Temporary fixture removed on exit.")

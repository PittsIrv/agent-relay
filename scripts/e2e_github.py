#!/usr/bin/env python3
"""End-to-end handoff scenarios over real GitHub, between two separate clones.

Usage:
    RELAY_E2E_REPO=owner/sandbox python scripts/e2e_github.py

Never run by the unit-test suite or CI: it does nothing unless RELAY_E2E_REPO is set.

What it expects
- A private scratch repository you own, named in RELAY_E2E_REPO (it may be empty).
  Its name must contain sandbox, scratch, e2e or test. The script pushes `main` if missing,
  and at the start and end of every run closes the issues and pull requests Relay opened
  (ownership marker, `relay/*` heads) and deletes `relay/*` branches, in that repository only.
  Do not point it at a repository where `relay/*` branches matter.
- `gh` signed in with write access to it, and git able to push over https.
- No model calls: agents are the offline mock fixtures.

Limits
- Both clones act through the one GitHub login that `gh` is signed in as. They differ by
  relay identity (owner and worker id) only. Scenarios that need two GitHub logins, such
  as write access being revoked, or an issue assigned to another login, are covered by the
  fake-gh unit tests, not here.
- The Claude Code usage-limit hook is exercised with a synthetic StopFailure payload, not a
  real rate-limit event.

What it emits: one line per check, a pass count per scenario, and a JSON summary.
Exit code 0 only if every check passes.
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agent_relay import config, interactive  # noqa: E402
from agent_relay.demo import CHECK, mock  # noqa: E402
from agent_relay.github import GitHub  # noqa: E402
from agent_relay.gitstore import Store, git, output  # noqa: E402

quiet = lambda _: None
RESULTS = {}
CURRENT = None


def check(label, ok, detail=""):
    RESULTS.setdefault(CURRENT, []).append(bool(ok))
    print(f"  {'ok  ' if ok else 'FAIL'} {label}{'' if ok else f'  [{detail}]'}")
    return ok


def scenario(name):
    global CURRENT
    CURRENT = name
    print(f"\n{name}")


def gh(slug, method, path, body=None):
    argv = ["gh", "api", "--method", method, f"repos/{slug}" + (f"/{path}" if path else "")]
    data = None
    if body is not None:
        argv += ["--input", "-"]
        data = json.dumps(body).encode()
    run = subprocess.run(argv, input=data, capture_output=True, timeout=60)
    if run.returncode:
        raise RuntimeError(f"gh {method} {path}: {run.stderr.decode()[:200]}")
    text = run.stdout.decode().strip()
    return json.loads(text) if text else None


def listing(slug, path):
    page, items = 1, []
    while True:
        chunk = gh(slug, "GET", f"{path}{'&' if '?' in path else '?'}per_page=100&page={page}") or []
        items += chunk
        if len(chunk) < 100:
            return items
        page += 1


def cleanup(slug):
    """Leave the sandbox with nothing of Relay's open. Touches only what Relay created: issues
    carrying its ownership marker, pull requests from `relay/*` branches, and `relay/*` branches."""
    for item in listing(slug, "issues?state=open"):
        if "pull_request" not in item and "<!-- relay-task:" in (item.get("body") or ""):
            gh(slug, "PATCH", f"issues/{item['number']}", {"state": "closed"})
    for item in listing(slug, "pulls?state=open"):
        if item["head"]["ref"].startswith("relay/"):
            gh(slug, "PATCH", f"pulls/{item['number']}", {"state": "closed"})
    try:
        refs = gh(slug, "GET", "git/matching-refs/heads/relay/") or []
    except RuntimeError as exc:
        if "HTTP 409" not in str(exc):  # 409: the repository has no commits yet
            raise
        refs = []
    for ref in refs:
        gh(slug, "DELETE", f"git/{ref['ref']}")


def relay(repo, *args):
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    run = subprocess.run([sys.executable, "-m", "agent_relay", *args], cwd=repo, env=env,
                         capture_output=True, text=True, timeout=300)
    return run.returncode, run.stdout + run.stderr


def add_task(repo, prompt):
    code, out = relay(repo, "add", prompt, "--base", "main")
    found = re.search(r"Queued (\S+) as issue #(\d+)", out)
    check(f"relay add -> issue ({prompt[:28]}...)", code == 0 and found, out)
    return (found[1], int(found[2])) if found else (None, None)


def task_of(store, task_id):
    """The task as this clone sees it; before its first fetch, a stub so callers just see no status."""
    return store.read()[1]["tasks"].get(task_id) or {"status": None}


def issue_text(slug, number):
    return "\n".join(c["body"] for c in gh(slug, "GET", f"issues/{number}/comments?per_page=100"))


def bootstrap(slug, work):
    url = f"https://github.com/{slug}.git"
    seed = work / "seed"
    git(work, "clone", "--quiet", url, str(seed), check=False)
    if not (seed / ".git").exists():
        git(work, "init", "--quiet", "--initial-branch=main", str(seed))
        git(seed, "remote", "add", "origin", url)
    for key, value in (("user.name", "Relay E2E"), ("user.email", "e2e@example.invalid")):
        git(seed, "config", key, value)
    if not output(seed, "ls-remote", "--heads", "origin", "main"):
        (seed / "README.md").write_text("Scratch repository for Agent Relay end-to-end runs.\n")
        git(seed, "add", "README.md")
        git(seed, "commit", "--quiet", "-m", "Seed the sandbox")
        git(seed, "push", "--quiet", "origin", "main")
    clones = {}
    for name in ("alice", "bob"):
        clone = work / name
        git(work, "clone", "--quiet", url, str(clone))
        for key, value in (("user.name", "Relay E2E"), ("user.email", "e2e@example.invalid")):
            git(clone, "config", key, value)
        cfg = config.initialize(clone, "origin", CHECK, github=slug, owner=name, default_branch="main")
        cfg["mode"] = "pooled"
        config.save(clone, cfg)
        GitHub(slug).ensure_label()
        clones[name] = clone
    return clones["alice"], clones["bob"]


def main():
    slug = os.environ.get("RELAY_E2E_REPO")
    if not slug:
        print("Set RELAY_E2E_REPO=owner/scratch-repo to run the GitHub scenarios (nothing was called).")
        return 0
    info = gh(slug, "GET", "")
    if not info.get("private"):
        print(f"Refusing to run against a public repository: {slug}")
        return 2
    if not re.search(r"sandbox|scratch|e2e|test", slug.split("/")[-1], re.I):
        print(f"Refusing {slug}: the repository name must contain sandbox, scratch, e2e or test.")
        return 2
    with tempfile.TemporaryDirectory(prefix="relay-e2e-") as folder:
        work = Path(folder)
        cleanup(slug)
        alice, bob = bootstrap(slug, work)
        sa, sb = Store(alice), Store(bob)
        def guarded(fn):
            try:
                fn(slug, alice, bob, sa, sb)
            except Exception as exc:  # A broken scenario fails its own count; later ones still run.
                check(f"{fn.__name__} ran to the end", False, repr(exc))

        try:
            guarded(take_and_hand_back)
            guarded(supersede)
            guarded(cancel)
            guarded(usage_limit)
        finally:
            cleanup(slug)
    print("\nSUMMARY")
    for name, results in RESULTS.items():
        print(f"  {name}: {sum(results)}/{len(results)}")
    total = [ok for results in RESULTS.values() for ok in results]
    print(json.dumps({"result": "PASS" if all(total) else "FAIL",
                      "scenarios": {k: f"{sum(v)}/{len(v)}" for k, v in RESULTS.items()}}))
    return 0 if all(total) else 1


SHARED = {}


def take_and_hand_back(slug, alice, bob, sa, sb):
    scenario("1. take")
    tid, issue = add_task(alice, "Implement answer() returning 42")
    check("task is queued in shared state", task_of(sa, tid)["status"] == "queued")
    info = gh(slug, "GET", f"issues/{issue}")
    check("issue is open and labelled relay", info["state"] == "open" and
          any(label["name"] == "relay" for label in info["labels"]))
    code, out = relay(alice, "take", tid)
    check("alice takes it", code == 0, out)
    check("task is held by alice and bob's worker will not claim it", task_of(sb, tid)["status"] == "held")
    mock(bob, "finish-fresh", emit=quiet).run(once=True)
    check("bob's worker leaves a held task alone", task_of(sb, tid)["status"] == "held")
    check("take is narrated on the issue", "took" in issue_text(slug, issue).lower() or
          "hold" in issue_text(slug, issue).lower(), issue_text(slug, issue)[-300:])

    scenario("2. hand back")
    (alice / "answer.py").write_text("def answer():\n    # alice started this\n    return None\n")
    code, out = relay(alice, "handoff", "Finish answer() so it returns 42")
    check("alice hands the task back", code == 0, out)
    task = task_of(sb, tid)
    check("task is queued again with a checkpoint", task["status"] == "queued" and task.get("checkpoint"))
    check("checkpoint branch exists on GitHub", bool(output(bob, "ls-remote", "--heads", "origin",
                                                              task["checkpoint"]["branch"])))
    mock(bob, "finish", emit=quiet).run(once=True)
    task = task_of(sa, tid)
    check("bob's agent continued alice's partial work and the check passed",
          task["status"] == "review" and task["verification"]["result"] == "passed", task["status"])
    pr = gh(slug, "GET", f"pulls/{task['pr']}") if task.get("pr") else {}
    check("a draft PR is open for the task", pr.get("state") == "open" and pr.get("draft"))
    check("the PR closes the issue", f"Closes #{issue}" in (pr.get("body") or ""))
    SHARED.update(task=tid, pr=task.get("pr"), issue=issue)

    # A second, finished-by-the-human hand back (--review) on a fresh task.
    git(alice, "checkout", "--quiet", "main")  # A handoff leaves the sender's files in place; start clean.
    git(alice, "clean", "--quiet", "-fd")
    tid2, issue2 = add_task(alice, "Add a second helper, double()")
    code, out = relay(alice, "take", tid2)
    check("alice takes a second task", code == 0, out)
    (alice / "double.py").write_text("def double(x):\n    return 2 * x\n")
    code, out = relay(alice, "handoff", "--review", "Done by hand")
    task2 = task_of(sa, tid2)
    check("handoff --review sends the task straight to review with a PR",
          code == 0 and task2["status"] == "review" and task2.get("pr"), (task2["status"], task2.get("pr")))
    git(alice, "checkout", "--quiet", "main")


def supersede(slug, alice, bob, sa, sb):
    scenario("3. supersede")
    tid, old_pr = SHARED["task"], SHARED["pr"]
    code, out = relay(alice, "retry", tid, "Try again with the helper")
    check("alice requeues the reviewed task", code == 0, out)
    mock(bob, "finish-fresh", emit=quiet).run(once=True)
    task = task_of(sa, tid)
    check("a new attempt reaches review", task["status"] == "review", task["status"])
    old = gh(slug, "GET", f"pulls/{old_pr}")
    check("the superseded PR is closed", old["state"] == "closed")
    check("the new attempt has its own open PR", task.get("pr") and task["pr"] != old_pr and
          gh(slug, "GET", f"pulls/{task['pr']}")["state"] == "open")
    notes = gh(slug, "GET", f"issues/{old_pr}/comments?per_page=100")
    check("the old PR explains why it was closed", any("supersed" in c["body"].lower() or "retired" in c["body"].lower()
                                                       for c in notes), [c["body"][:80] for c in notes])


def cancel(slug, alice, bob, sa, sb):
    scenario("4. cancel")
    tid, issue = add_task(alice, "Run for a long time")
    worker = mock(bob, "wait", emit=print, sync=2, heartbeat=1, ttl=30)
    thread = threading.Thread(target=worker.run, kwargs={"once": True})
    thread.start()
    deadline = time.monotonic() + 60
    while task_of(sb, tid)["status"] != "running" and time.monotonic() < deadline:
        time.sleep(0.3)
    check("bob's agent is running it", task_of(sb, tid)["status"] == "running", task_of(sb, tid)["status"])
    gh(slug, "POST", f"issues/{issue}/comments", {"body": "/relay cancel"})
    thread.join(timeout=120)
    check("the running agent stopped on its own", not thread.is_alive())
    code, out = relay(alice, "sync")
    check("the cancel comment was applied", task_of(sa, tid)["status"] == "cancelled", task_of(sa, tid)["status"])
    check("cancellation is narrated on the issue", "cancel" in issue_text(slug, issue).lower())
    check("no PR was opened for it", not task_of(sa, tid).get("pr"))
    mock(bob, "finish-fresh", emit=quiet).run(once=True)
    check("a cancelled task is not claimed again", task_of(sb, tid)["status"] == "cancelled")


def usage_limit(slug, alice, bob, sa, sb):
    scenario("5. usage-limit handoff")
    tid, issue = add_task(alice, "Implement answer() so it returns 42 (worker hits its limit)")
    mock(alice, "limit", emit=quiet).run(once=True)
    task = task_of(sa, tid)
    check("alice's worker published a checkpoint and requeued", task["status"] == "queued" and task.get("checkpoint"),
          task["status"])
    check("alice's worker is cooling down", config.load(alice).get("cooldown_until", 0) > time.time())
    mock(bob, "finish", emit=quiet).run(once=True)
    task = task_of(sb, tid)
    check("bob resumed from alice's partial work and the check passed",
          task["status"] == "review" and task["verification"]["result"] == "passed", task["status"])
    check("handoff is visible on the issue", "usage limit" in issue_text(slug, issue).lower(),
          issue_text(slug, issue)[-300:])

    # The Claude Code hook path, driven by a synthetic StopFailure payload.
    git(alice, "checkout", "--quiet", "main")
    git(alice, "clean", "--quiet", "-fd")
    (alice / "answer.py").write_text("def answer():\n    # cut off by the session limit\n    return None\n")
    transcript = alice.parent / "session.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content":
                                      "Implement answer() so it returns 42"}}) + "\n")
    config.update(alice, share_prompt=True)
    hook = {"hook_event_name": "StopFailure", "error": "rate_limit", "cwd": str(alice),
            "transcript_path": str(transcript)}
    task = interactive.run_hook(stdin=io.StringIO(json.dumps(hook)), emit=quiet)
    check("the hook published the session's work as a task", bool(task) and task["status"] == "queued")
    check("alice's own files were left untouched", output(alice, "status", "--porcelain") == "?? answer.py")
    mock(bob, "finish", emit=quiet).run(once=True)
    done = task_of(sb, task["id"]) if task else {}
    check("bob finished the hook-published task", done.get("status") == "review", done.get("status"))


if __name__ == "__main__":
    sys.exit(main())

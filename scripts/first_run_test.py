#!/usr/bin/env python3
"""A stranger's first handoff: follow only the README, in a clean directory, over real GitHub.

Usage:
    RELAY_E2E_REPO=owner/sandbox python scripts/first_run_test.py

Never run by the unit-test suite or CI: it does nothing unless RELAY_E2E_REPO is set (a
private scratch repo whose name contains sandbox, scratch, e2e or test, like e2e_github.py).

What it does
- Makes an empty temp directory and clones the repo with the README's own clone command
  (OWNER/REPO filled in); nothing from this checkout is used. The tool is installed with the install command the README gives (pipx, else the
  README's venv/pip alternative), into the temp directory.
- Reads the commands out of the README's code blocks and runs them literally, in the order a
  new user would: init, add, status (to learn the task id), take, hand back with handoff.
  If the README lacks a command the path needs, that is a gap and the run fails.
- Fails on any command that exits non-zero, or whose error output gives no next step.
- Records the wall-clock minutes of the commands from the start to the handed-back task
  (the target is 10 or less; time spent reading is not counted).

Options (environment)
- RELAY_FIRST_RUN_SOURCE=/path/to/checkout installs from a checkout instead of the git URL
  in the README, to test a README change before it is merged. The default is the README as
  published.

Limits: one GitHub login, signed in through `gh`; no model calls; the real hook is not run.
Exit code 0 only if every step passes.
"""

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
README = (ROOT / "README.md").read_text()
FAILS = []
STEPS = []


def readme_commands():
    """Every command in the README, in document order: lines of sh code blocks and inline code
    spans that start with `relay `."""
    lines = []
    for match in re.finditer(r"```sh\n(.*?)```|`(relay [^`]+)`", README, re.S):
        if match[1] is not None:
            lines += [x.strip() for x in match[1].splitlines() if x.strip() and not x.lstrip().startswith("#")]
        else:
            lines.append(match[2].strip())
    return lines


def find(prefix):
    for line in readme_commands():
        if line.startswith(prefix):
            return line
    return None


def step(label, command, cwd, env, expect_ok=True):
    started = time.monotonic()
    run = subprocess.run(command, shell=True, cwd=cwd, env=env, capture_output=True, text=True, timeout=180)
    took = time.monotonic() - started
    out = (run.stdout + run.stderr).strip()
    ok = (run.returncode == 0) == expect_ok
    STEPS.append((label, command, took, ok))
    print(f"  {'ok  ' if ok else 'FAIL'} {label}  ({took:.1f}s)  $ {command}")
    if not ok:
        FAILS.append(f"{label}: {out[-400:]}")
        print("       " + out[-400:].replace("\n", "\n       "))
    return run.returncode, out


def need(label, prefix):
    line = find(prefix)
    if not line:
        FAILS.append(f"README has no `{prefix}…` command for: {label}")
        print(f"  FAIL README gap: no `{prefix}…` command for: {label}")
    return line


def main():
    slug = os.environ.get("RELAY_E2E_REPO")
    if not slug:
        print("Set RELAY_E2E_REPO=owner/scratch-repo to run the first-run test (nothing was called).")
        return 0
    sys.path.insert(0, str(ROOT / "scripts"))
    import e2e_github as sandbox

    info = sandbox.gh(slug, "GET", "")
    if not info.get("private") or not re.search(r"sandbox|scratch|e2e|test", slug.split("/")[-1], re.I):
        print(f"Refusing {slug}: it must be a private repository named like a sandbox.")
        return 2
    start = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="relay-first-run-") as folder:
        work = Path(folder)
        env = {**os.environ, "PIPX_HOME": str(work / "pipx"), "PIPX_BIN_DIR": str(work / "bin"),
               "PATH": f"{work / 'bin'}:{work / 'venv' / 'bin'}:{os.environ['PATH']}", "GIT_TERMINAL_PROMPT": "0"}
        env.pop("PYTHONPATH", None)
        sandbox.cleanup(slug)
        try:
            first_run(slug, work, env)
        finally:
            sandbox.cleanup(slug)
    minutes = (time.monotonic() - start) / 60
    commands = sum(took for _, _, took, _ in STEPS) / 60
    print(f"\nminutes to first handoff: {commands:.1f} of commands ({minutes:.1f} wall incl. setup/cleanup); target <= 10")
    print(json.dumps({"result": "PASS" if not FAILS and commands <= 10 else "FAIL",
                      "steps": len(STEPS), "minutes_commands": round(commands, 2), "gaps": FAILS}))
    return 0 if not FAILS and commands <= 10 else 1


def first_run(slug, work, env):
    source = os.environ.get("RELAY_FIRST_RUN_SOURCE")
    install = None
    for line in readme_commands():
        if "git+https://github.com/" in line and subprocess.run(
                f"command -v {shlex.split(line)[0]}", shell=True, env=env, capture_output=True).returncode == 0:
            install = line
            break
    if not install:
        FAILS.append("README's install command needs a program this machine lacks, and it offers no alternative")
        print("  FAIL README gap: no usable install command (pipx missing and no pip/venv alternative)")
        return
    if source:
        install = re.sub(r"git\+https://github.com/[^'\" ]+", source, install)
    if "venv" in install:  # The README's venv alternative installs into a folder under the home directory.
        install = install.replace("~/.relay", str(work / "venv")).replace("$HOME/.relay", str(work / "venv"))
    step("install the tool", install, work, env)
    step("the tool runs", "relay --version", work, env)
    clone = need("clone the shared repository", "gh repo clone")
    if not clone:
        return
    name = slug.split("/")[-1]
    literal = re.sub(r"\bREPO\b", name, clone.replace("OWNER/REPO", slug))
    step("fresh clone of the repository", literal, work, env)
    project = work / name
    step("git identity for commits", "git config user.name 'First Run' && git config user.email first@example.invalid",
         project, env)
    step("gh is signed in", "gh auth status", project, env)

    init = need("enroll the clone", "relay init")
    add = need("ask for help from the terminal", "relay add")
    status = need("find the task id", "relay status")
    take = need("take the task", "relay take")
    handoff = need("hand back", "relay handoff")
    if not all((init, add, status, take, handoff)):
        return
    step("relay init", init, project, env)
    code, out = step("relay add", add, project, env)
    code, out = step("relay status", status, project, env)
    found = re.search(r"^\s+([0-9a-f]{10})\s+queued", out, re.M)
    if not found:
        FAILS.append("relay status did not show the new task with its id")
        print("  FAIL relay status showed no task id")
        return
    step("relay take", re.sub(r"\bTASK\b", found[1], take), project, env)
    (project / "notes.txt").write_text("A first user's unfinished edit.\n")
    step("relay handoff", handoff, project, env)
    _, out = step("relay status after hand back", status, project, env)
    if not re.search(rf"{found[1]}\s+queued\s+[0-9a-f]{{7}}", out):
        FAILS.append("after the hand back the task is not queued with a checkpoint")
        print("  FAIL the handed-back task is not queued with a checkpoint")
    issues = sandbox_issue_text(slug)
    if "usage limit" not in issues.lower() and "hand" not in issues.lower():
        FAILS.append("the issue thread does not show the hand back")
        print("  FAIL the issue thread does not show the hand back")


def sandbox_issue_text(slug):
    import e2e_github as sandbox
    text = []
    for issue in sandbox.listing(slug, "issues?state=all"):
        text.append(sandbox.issue_text(slug, issue["number"]))
    return "\n".join(text)


if __name__ == "__main__":
    sys.exit(main())

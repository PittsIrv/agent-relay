"""`relay doctor`: check what a first handoff needs and say how to fix each failure.

Read-only. It never prints a token, a remote URL or the output of `gh auth status`: it
uses exit codes and version strings, and redacts credentials from anything it shows.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import config
from .github import SLUG, GitHub
from .gitstore import RelayError, Store
from .interactive import HOOK_TAG

MIN_GIT = (2, 31)  # `rev-parse --path-format=absolute`
SKEW_WARN, SKEW_FAIL = 30, 120  # seconds; leases use the wall clock
SECRET = re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w+|sk-[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,})")
TOKEN_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")


def redact(text, env=os.environ):
    text = SECRET.sub("[redacted]", str(text))
    text = re.sub(r"(https?)://[^@/\s]+@", r"\1://", text)  # credentials inside an http(s) remote URL
    for name in TOKEN_ENV:
        if len(env.get(name) or "") >= 8:
            text = text.replace(env[name], "[redacted]")
    return text


def server_time():
    """Epoch seconds from GitHub's Date header, or None when it cannot be reached."""
    try:
        request = urllib.request.Request("https://api.github.com/", method="HEAD")
        with urllib.request.urlopen(request, timeout=10) as reply:
            return parsedate_to_datetime(reply.headers["Date"]).timestamp()
    except Exception:
        return None


class Report:
    def __init__(self, env):
        self.items = []
        self.env = env

    def add(self, name, status, detail="", fix=""):
        self.items.append({"name": name, "status": status, "detail": redact(detail, self.env),
                           "fix": redact(fix, self.env)})
        return status == "ok"

    def failed(self):
        return any(item["status"] == "fail" for item in self.items)


def run_git(start, *args, timeout=30):
    try:
        return subprocess.run(["git", *args], cwd=start, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None


def check(start=".", env=None, which=shutil.which, clock=server_time):
    env = dict(os.environ if env is None else env)
    report = Report(env)
    add = report.add
    if os.name != "posix":
        add("platform", "fail", f"{sys.platform} is not supported",
            "Relay needs macOS or Linux (workers use POSIX process groups). On Windows, use WSL2.")
        return report
    add("platform", "ok", sys.platform)
    if sys.version_info < (3, 11):
        add("python", "fail", sys.version.split()[0], "Install Python 3.11 or newer.")
    else:
        add("python", "ok", sys.version.split()[0])

    version = run_git(start, "--version")
    found = re.search(r"(\d+)\.(\d+)", version.stdout) if version and version.returncode == 0 else None
    if not found:
        add("git", "fail", "git is not installed", "Install Git: https://git-scm.com/downloads")
        return report
    if (int(found[1]), int(found[2])) < MIN_GIT:
        add("git", "fail", f"git {found[0]} is too old", f"Upgrade Git to {MIN_GIT[0]}.{MIN_GIT[1]} or newer.")
    else:
        add("git", "ok", version.stdout.strip())

    top = run_git(start, "rev-parse", "--show-toplevel")
    if not top or top.returncode:
        add("repository", "fail", "this folder is not inside a Git repository",
            "cd into your clone of the shared repository, then run `relay doctor` again.")
        return report
    repo = Path(top.stdout.strip())
    add("repository", "ok", repo.name)

    who = [(run_git(repo, "config", key) or subprocess.CompletedProcess([], 1, "")).stdout.strip()
           for key in ("user.name", "user.email")]
    if all(who):
        add("git identity", "ok", "user.name and user.email are set")
    else:
        add("git identity", "fail", "user.name or user.email is not set",
            "git config --global user.name 'Your Name' && git config --global user.email you@example.com")

    try:
        cfg = config.load(repo)
        add("relay init", "ok", f"enrolled as {cfg.get('owner') or cfg['worker']} ({cfg['mode']})")
        if cfg["mode"] == "paused":
            add("worker", "ok", "paused: `relay start` opts this clone in to taking work")
    except RelayError:
        cfg = {}
        add("relay init", "fail", "this clone is not enrolled",
            "relay init   (add --check '<your test command>' to verify finished work)")
    remotes = (run_git(repo, "remote") or subprocess.CompletedProcess([], 1, "")).stdout.split()
    remote = cfg.get("remote") or ("origin" if "origin" in remotes or len(remotes) != 1 else remotes[0])

    if remote not in remotes:
        add("remote", "fail", f"no Git remote named {remote!r}", f"git remote add {remote} <url>")
        return report
    # The configured URL as written (get-url would expand `insteadOf` rewrites).
    url = (run_git(repo, "config", "--get", f"remote.{remote}.url") or subprocess.CompletedProcess([], 1, "")).stdout.strip()
    slug = cfg.get("github") or (f"{SLUG.search(url)[1]}/{SLUG.search(url)[2]}" if SLUG.search(url) else None)
    reach = run_git(repo, "ls-remote", "--heads", remote)
    if not reach or reach.returncode:
        add("remote access", "fail", f"cannot read {slug or remote}",
            f"Check your network and Git credentials: git ls-remote {remote}")
        return report
    add("remote access", "ok", slug or remote)

    on_github = bool(SLUG.search(url))
    custom = env.get("RELAY_GH")
    found_gh = which("gh")
    argv = cfg.get("gh") or (shlex.split(custom) if custom else ([found_gh] if found_gh else None))
    gh_ok = False
    if on_github:
        if not argv:
            add("gh installed", "fail", "the GitHub CLI is not installed",
                "Install gh: https://cli.github.com , then run `gh auth login`.")
        else:
            add("gh installed", "ok", "found")
            try:
                signed = subprocess.run([*argv, "auth", "status"], capture_output=True, timeout=30,
                                        env=env).returncode == 0
            except (OSError, subprocess.TimeoutExpired):
                signed = False
            if signed:
                gh_ok = add("gh signed in", "ok", "signed in")
            else:
                add("gh signed in", "fail", "gh is not signed in", "gh auth login")
        if gh_ok and not cfg.get("github"):
            add("github conversation", "warn", "GitHub mirroring is off for this clone",
                "relay init   (run again now that gh is signed in)")
        elif cfg.get("github"):
            add("github conversation", "ok", f"mirroring tasks to {cfg['github']}")
    else:
        add("gh", "ok", "not needed: the remote is not on github.com")

    pushed = None
    if gh_ok and slug:
        try:
            info = GitHub(slug, argv).api("GET", f"repos/{slug}")
            pushed = bool((info or {}).get("permissions", {}).get("push"))
        except RelayError:
            pushed = None
    if pushed is None:
        probe = run_git(repo, "push", "--dry-run", remote, "HEAD:refs/heads/relay-doctor-probe")
        pushed = bool(probe and probe.returncode == 0)
    if pushed:
        add("push access", "ok", "you can push to the shared repository")
    elif not (run_git(repo, "rev-parse", "--verify", "--quiet", "HEAD") or subprocess.CompletedProcess([], 1)).returncode == 0:
        add("push access", "fail", "this clone has no commits yet to push from",
            "Fetch the shared branch (git pull) or make a first commit, then run `relay doctor` again.")
    else:
        add("push access", "fail", "no push access to the shared repository",
            "Ask the owner for write access, or check the `git push` credentials for this remote.")

    refs = (run_git(repo, "ls-remote", "--refs", remote, "refs/heads/relay/state")
            or subprocess.CompletedProcess([], 1, "")).stdout.strip()
    if not refs:
        add("relay/state", "ok", "not created yet: the first `relay add` creates it")
    else:
        try:
            Store(repo, remote).read()
            add("relay/state", "ok", "present and readable")
        except RelayError as exc:
            add("relay/state", "fail", str(exc), "Everyone on the repository should run the same Relay version: "
                "upgrade with pipx upgrade agent-relay-cli, or reinstall from the repository.")

    agents = [name for name in ("claude", "codex") if which(name)]
    if agents:
        add("agent CLI", "ok", ", ".join(agents) + " on PATH")
    else:
        add("agent CLI", "warn", "neither claude nor codex is on PATH",
            "Only needed for `relay start`. Install Claude Code or the Codex CLI and sign in.")

    settings = repo / ".claude" / "settings.local.json"
    try:
        hooked = HOOK_TAG in settings.read_text()
    except OSError:
        hooked = False
    if hooked:
        add("usage-limit hook", "ok", "installed in this clone")
    else:
        add("usage-limit hook", "warn", "not installed (optional)", "relay hook install")

    if on_github:
        stamp = clock()
        if stamp is None:
            add("clock", "warn", "could not compare with GitHub's clock", "Check that your clock is correct.")
        else:
            skew = abs(time.time() - stamp)
            if skew > SKEW_FAIL:
                add("clock", "fail", f"your clock is {int(skew)}s off", "Turn on automatic time in your system settings. "
                    "Leases use the wall clock.")
            elif skew > SKEW_WARN:
                add("clock", "warn", f"your clock is {int(skew)}s off", "Turn on automatic time in your system settings.")
            else:
                add("clock", "ok", f"within {int(skew)}s of GitHub")

    tree = run_git(repo, "status", "--porcelain")
    if tree and tree.stdout.strip():
        add("working tree", "warn", "you have uncommitted changes",
            "`relay take` needs a clean tree: commit or stash first. (`relay handoff` is fine.)")
    return report


def render(report, as_json=False):
    if as_json:
        return json.dumps({"ok": not report.failed(), "checks": report.items}, indent=2)
    marks = {"ok": "ok  ", "warn": "warn", "fail": "FAIL"}
    lines = []
    for item in report.items:
        line = f"{marks[item['status']]} {item['name']}: {item['detail']}"
        lines.append(line)
        if item["status"] != "ok" and item["fix"]:
            lines.append(f"       fix: {item['fix']}")
    bad = sum(item["status"] == "fail" for item in report.items)
    lines.append("")
    lines.append("Ready for a first handoff." if not bad else f"{bad} problem(s) to fix before a first handoff.")
    return "\n".join(lines)

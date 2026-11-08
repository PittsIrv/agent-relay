import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_relay import config, doctor
from agent_relay.demo import setup
from agent_relay.gitstore import git

SRC = str(Path(__file__).resolve().parent.parent / "src")
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6"


def github_clone(root, owner="alice", url="https://github.com/demo/shared.git"):
    """A clone whose remote URL names github.com but resolves to the local bare repo."""
    a, b, _ = setup(root, github=True)
    repo = a if owner == "alice" else b
    git(repo, "remote", "set-url", "origin", url)
    git(repo, "config", f"url.{Path(root) / 'shared.git'}.insteadOf", url)
    return repo


def everything(name):
    return f"/usr/bin/{name}"


def now():
    return time.time()


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def run_doctor(self, repo, **kwargs):
        kwargs.setdefault("which", everything)
        kwargs.setdefault("clock", now)
        kwargs.setdefault("env", {"PATH": os.environ["PATH"]})
        return doctor.check(repo, **kwargs)

    def status(self, report):
        return {item["name"]: item for item in report.items}

    def test_good_setup_passes_with_no_failures(self):
        repo = github_clone(self.root)
        report = self.run_doctor(repo)
        self.assertFalse(report.failed(), doctor.render(report))
        names = self.status(report)
        for name in ("platform", "git", "repository", "relay init", "remote access", "gh signed in",
                     "push access", "relay/state", "agent CLI", "clock"):
            self.assertEqual(names[name]["status"], "ok", name)
        self.assertIn("Ready for a first handoff.", doctor.render(report))

    def test_each_broken_setup_names_its_fix(self):
        repo = github_clone(self.root)
        cases = []

        outside = self.root / "not-a-repo"
        outside.mkdir()
        cases.append(("repository", self.run_doctor(outside), "cd into your clone"))

        fresh = self.root / "fresh"
        git(self.root, "clone", "--quiet", str(self.root / "shared.git"), str(fresh))
        cases.append(("relay init", self.run_doctor(fresh), "relay init"))

        cfg = config.load(repo)
        config.update(repo, gh=["sh", "-c", "exit 1"])
        cases.append(("gh signed in", self.run_doctor(repo), "gh auth login"))
        config.update(repo, gh=cfg["gh"])

        config.update(repo, gh=None)
        cases.append(("gh installed", self.run_doctor(repo, which=lambda n: None if n == "gh" else everything(n)),
                      "https://cli.github.com"))
        config.update(repo, gh=cfg["gh"])

        db = self.root / "github.json"
        data = json.loads(db.read_text())
        data["permissions"]["alice"] = "read"
        db.write_text(json.dumps(data))
        cases.append(("push access", self.run_doctor(repo), "write access"))

        cases.append(("clock", self.run_doctor(repo, clock=lambda: time.time() - 600), "automatic time"))

        git(repo, "remote", "rename", "origin", "elsewhere")
        cases.append(("remote", self.run_doctor(repo), "git remote add origin"))

        for name, report, fix in cases:
            item = self.status(report)[name]
            self.assertEqual(item["status"], "fail", f"{name}: {doctor.render(report)}")
            self.assertIn(fix, item["fix"], name)
            self.assertNotIn("python -m unittest", item["fix"], name)
        self.assertTrue(all(report.failed() for _, report, _ in cases))

    def test_optional_problems_warn_without_failing(self):
        repo = github_clone(self.root)
        (repo / "scratch.txt").write_text("uncommitted\n")
        report = self.run_doctor(repo, which=lambda n: None, clock=lambda: None)
        names = self.status(report)
        self.assertFalse(report.failed(), doctor.render(report))
        for name in ("agent CLI", "usage-limit hook", "working tree", "clock"):
            self.assertEqual(names[name]["status"], "warn", name)

    def test_windows_gets_a_clear_message(self):
        with mock.patch.object(doctor.os, "name", "nt"):
            report = doctor.check(self.root, which=everything, clock=now)
        self.assertTrue(report.failed())
        self.assertIn("WSL2", report.items[0]["fix"])

    def test_no_token_or_credentialed_url_is_ever_printed(self):
        secret_url = "https://someone:SECRETPASS@github.com/demo/shared.git"
        repo = github_clone(self.root, url=secret_url)
        leak = ["sh", "-c", f"echo {TOKEN}; echo {TOKEN} >&2; exit 1"]
        config.update(repo, gh=leak)
        env = {"PATH": os.environ["PATH"], "GH_TOKEN": TOKEN}
        report = self.run_doctor(repo, env=env)
        for text in (doctor.render(report), doctor.render(report, as_json=True)):
            self.assertNotIn(TOKEN, text)
            self.assertNotIn("SECRETPASS", text)
        self.assertEqual(self.status(report)["gh signed in"]["status"], "fail")
        self.assertEqual(doctor.redact(f"token {TOKEN} at https://u:p@github.com/x", {}), "token [redacted] at https://github.com/x")
        self.assertEqual(doctor.redact("ssh://git@github.com/x", {}), "ssh://git@github.com/x")
        self.assertEqual(doctor.redact("a false b", {"GH_TOKEN": "a"}), "a false b")

    def test_cli_exit_codes_and_json(self):
        repo = github_clone(self.root)
        env = {**os.environ, "PYTHONPATH": SRC}
        run = subprocess.run([sys.executable, "-m", "agent_relay", "doctor", "--json"], cwd=repo, env=env,
                             capture_output=True, text=True)
        data = json.loads(run.stdout)
        self.assertEqual(run.returncode, 0 if data["ok"] else 1)
        self.assertTrue({"name", "status", "detail", "fix"} <= set(data["checks"][0]))
        outside = self.root / "nowhere"
        outside.mkdir()
        run = subprocess.run([sys.executable, "-m", "agent_relay", "doctor"], cwd=outside, env=env,
                             capture_output=True, text=True)
        self.assertEqual(run.returncode, 1)
        self.assertIn("fix:", run.stdout)


if __name__ == "__main__":
    unittest.main()

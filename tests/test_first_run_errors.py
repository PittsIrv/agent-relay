import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_relay.demo import setup

SRC = str(Path(__file__).resolve().parent.parent / "src")


def relay(cwd, *args):
    env = {**os.environ, "PYTHONPATH": SRC}
    run = subprocess.run([sys.executable, "-m", "agent_relay", *args], cwd=cwd, env=env,
                         capture_output=True, text=True)
    return run.returncode, run.stdout + run.stderr


class FirstTimerErrorsSayWhatToDo(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_outside_a_repository(self):
        code, out = relay(self.root, "init")
        self.assertNotEqual(code, 0)
        self.assertIn("cd into your clone", out)

    def test_repository_without_the_remote(self):
        subprocess.run(["git", "init", "-q", str(self.root / "r")], check=True)
        code, out = relay(self.root / "r", "init")
        self.assertNotEqual(code, 0)
        self.assertIn("git remote add origin <url>", out)

    def test_add_before_init_and_on_an_unpushed_branch(self):
        a, _, _ = setup(self.root)
        fresh = self.root / "fresh"  # A clone that has never been enrolled.
        subprocess.run(["git", "clone", "-q", str(self.root / "shared.git"), str(fresh)], check=True)
        code, out = relay(fresh, "add", "x")
        self.assertNotEqual(code, 0)
        self.assertIn("relay init", out)
        relay(a, "init", "--no-github")
        subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=a, check=True)
        code, out = relay(a, "add", "x")
        self.assertNotEqual(code, 0)
        self.assertIn("git push -u origin feature", out)
        self.assertIn("--base main", out)

    def test_unknown_task_points_at_status(self):
        a, _, _ = setup(self.root)
        relay(a, "init", "--no-github")
        for verb in ("take", "show", "cancel", "retry"):
            code, out = relay(a, verb, "nope")
            self.assertNotEqual(code, 0, verb)
            self.assertIn("relay status", out, verb)

    def test_init_says_how_to_turn_github_on(self):
        a, _, _ = setup(self.root)  # A local bare remote: no github.com remote to mirror to.
        code, out = relay(a, "init")
        self.assertEqual(code, 0)
        self.assertIn("gh auth login", out)
        code, out = relay(a, "init", "--no-github")
        self.assertNotIn("gh auth login", out)


if __name__ == "__main__":
    unittest.main()

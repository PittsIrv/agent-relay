import concurrent.futures
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from agent_relay import adapters, config, tasks
from agent_relay.checkpoint import snapshot
from agent_relay.demo import mock, setup
from agent_relay.gitstore import RelayError, Store, output, private_dir


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.a, self.b, self.base = setup(self.temp.name)
        self.store = Store(self.a)
        self.task = tasks.add(self.store, "Implement answer() returning 42", self.base)

    def task_state(self):
        return self.store.read()[1]["tasks"][self.task]

    def test_two_clone_quota_handoff(self):
        mock(self.a, "limit", emit=lambda _: None).run(once=True)
        first = self.task_state()
        self.assertEqual(first["status"], "queued")
        self.assertIn("return None", output(self.a, "show", f"{first['checkpoint']['sha']}:answer.py"))
        mock(self.b, "finish", emit=lambda _: None).run(once=True)
        final = self.task_state()
        self.assertEqual(final["status"], "review")
        self.assertEqual(final["epoch"], 2)
        self.assertEqual(final["verification"]["result"], "passed")
        self.assertEqual(output(self.a, "ls-remote", "origin", "refs/heads/main").split()[0], self.base)
        self.assertEqual(output(self.a, "status", "--porcelain"), "")

    def test_competing_workers_get_one_lease(self):
        barrier = threading.Barrier(2)
        def run(repo, name):
            barrier.wait()
            return tasks.claim(Store(repo), name)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            a = pool.submit(run, self.a, "a")
            b = pool.submit(run, self.b, "b")
            results = [a.result(), b.result()]
        self.assertEqual(sum(value is not None for value in results), 1)
        self.assertEqual(self.task_state()["epoch"], 1)

    def test_expired_worker_cannot_publish(self):
        tasks.claim(self.store, "a", ttl=10, now=100)
        second = tasks.claim(Store(self.b), "b", ttl=10, now=111)
        self.assertEqual(second["epoch"], 2)
        with self.assertRaisesRegex(RelayError, "lease lost"):
            tasks.update(self.store, self.task, "a", 1, now=112, status="review")
        self.assertEqual(self.task_state()["lease"]["worker"], "b")

    def test_secret_file_never_pushed(self):
        with self.assertRaisesRegex(RelayError, "sensitive filename"):
            mock(self.a, "secret", emit=lambda _: None).run(once=True)
        self.assertEqual(self.task_state()["status"], "blocked")
        self.assertIsNone(self.task_state()["checkpoint"])
        self.assertEqual(output(self.a, "ls-remote", "origin", "refs/heads/relay/checkpoints/*"), "")
        self.assertTrue(list(private_dir(self.a).glob("attempts/*/worktree/.env")))

    def test_pause_releases_partial_work(self):
        worker = mock(self.a, "wait", emit=lambda _: None)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(worker.run, True)
            deadline = time.monotonic() + 10
            while not list(private_dir(self.a).glob("attempts/*/worktree/partial.txt")):
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            cfg = config.load(self.a)
            cfg["mode"] = "paused"
            config.save(self.a, cfg)
            future.result(timeout=15)
        state = self.task_state()
        self.assertEqual(state["status"], "queued")
        self.assertIn("recover me", output(self.a, "show", f"{state['checkpoint']['sha']}:partial.txt"))

    def test_paused_worker_does_not_claim(self):
        cfg = config.load(self.a)
        cfg["mode"] = "paused"
        config.save(self.a, cfg)
        mock(self.a, "limit", emit=lambda _: None).run(once=True)
        self.assertEqual(self.task_state()["epoch"], 0)

    def test_auth_failure_blocks_instead_of_repeating(self):
        mock(self.a, "error", emit=lambda _: None).run(once=True)
        self.assertEqual(self.task_state()["status"], "blocked")
        self.assertIsNone(tasks.claim(Store(self.b), "b"))

    def test_snapshot_preserves_user_index(self):
        (self.a / "new.py").write_text("print('hello')\n")
        before = output(self.a, "status", "--porcelain")
        sha = snapshot(self.a, self.base, self.task, 1)
        self.assertIn("hello", output(self.a, "show", f"{sha}:new.py"))
        self.assertEqual(output(self.a, "status", "--porcelain"), before)
        self.assertEqual(output(self.a, "rev-parse", "HEAD"), self.base)

    def test_error_classifier_ignores_ordinary_text(self):
        prose = json.dumps({"type": "assistant", "message": "handle rate_limit in the code"})
        self.assertIsNone(adapters.failure_kind(prose, 0))
        self.assertEqual(adapters.failure_kind("unexpected failure", 1), "error")
        self.assertEqual(adapters.failure_kind('{"type":"result","is_error":true,"result":"You have hit your limit"}', 0), "quota")

    def test_failed_push_retains_local_recovery_without_advertising_it(self):
        with patch("agent_relay.worker.publish", side_effect=RelayError("simulated network outage")):
            with self.assertRaisesRegex(RelayError, "network outage"):
                mock(self.a, "limit", emit=lambda _: None).run(once=True)
        self.assertIsNone(self.task_state()["checkpoint"])
        recovery = list(private_dir(self.a).glob("attempts/*/recovery.json"))
        self.assertEqual(len(recovery), 1)
        sha = json.loads(recovery[0].read_text())["sha"]
        self.assertIn("return None", output(self.a, "show", f"{sha}:answer.py"))

    def test_cache_is_not_in_checkpoint(self):
        cache = self.a / "__pycache__"
        cache.mkdir()
        (cache / "generated.pyc").write_bytes(b"generated")
        sha = snapshot(self.a, self.base, self.task, 1)
        self.assertEqual(sha, self.base)

    def test_retry_preserves_checkpoint(self):
        mock(self.a, "error", emit=lambda _: None).run(once=True)
        cp = self.task_state()["checkpoint"]
        tasks.retry(self.store, self.task)
        self.assertEqual(self.task_state()["status"], "queued")
        self.assertEqual(self.task_state()["checkpoint"], cp)


if __name__ == "__main__":
    unittest.main()

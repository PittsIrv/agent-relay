import collections
import concurrent.futures
import fcntl
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_relay import config, tasks
from agent_relay.conversation import Conversation
from agent_relay.demo import mock, setup
from agent_relay.gitstore import Store

quiet = lambda _: None


class TwoWorkersWithSlowGitHub(unittest.TestCase):
    """Two clones act at the same moment while every GitHub call takes up to a second."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.a, self.b, self.base = setup(self.root, github=True, latency=0.8)
        self.store = Store(self.a)

    def db(self):
        return json.loads((self.root / "github.json").read_text())

    def test_each_task_is_taken_once_and_narrated_once(self):
        cfg = config.load(self.a)
        ids = [tasks.add(self.store, f"Implement answer() returning 42 (#{n})", self.base, base_branch="main",
                         created_by=cfg["worker"], by="alice", requested_by="alice") for n in range(4)]
        workers = [mock(repo, "finish-fresh", emit=quiet, sync=0.1, heartbeat=1, ttl=30) for repo in (self.a, self.b)]

        def settled():
            found = self.store.read()[1]["tasks"]
            return all(found[i]["status"] == "review" and found[i].get("pr") for i in ids)

        def stop_when_done():
            try:
                deadline = time.monotonic() + 150
                while not settled() and time.monotonic() < deadline:
                    time.sleep(0.2)
            finally:
                for worker in workers:
                    worker.stopping = True

        watcher = threading.Thread(target=stop_when_done)
        watcher.start()
        with concurrent.futures.ThreadPoolExecutor() as pool:
            for future in [pool.submit(w.run) for w in workers]:
                future.result(timeout=200)
        watcher.join()

        found = self.store.read()[1]["tasks"]
        claimed = collections.Counter()
        for task_id in ids:
            self.assertEqual(found[task_id]["status"], "review", found[task_id]["status"])
            attempts = [e for e in found[task_id]["events"] if e["kind"] == "claimed"]
            self.assertEqual(len(attempts), 1, "a task must be claimed by exactly one worker")
            claimed[attempts[0]["worker"]] += 1
        self.assertEqual(sum(claimed.values()), 4)

        db = self.db()
        self.assertEqual(len(db["issues"]), 4, "one issue per task")
        pulls = db["pulls"]
        self.assertEqual(len(pulls), 4, "one PR per task")
        self.assertEqual(len({p["head"] for p in pulls.values()}), 4)
        self.assertTrue(all(p["state"] == "open" for p in pulls.values()))
        for number in db["issues"]:
            bodies = [c["body"] for c in db["comments"] if c["issue_url"].endswith(f"/issues/{number}")]
            self.assertEqual(len(bodies), len(set(bodies)), f"duplicate comment on issue #{number}")

    def test_simultaneous_sync_imports_each_issue_once(self):
        def edit(db):
            for n in (1, 2, 3):
                db["issues"][str(n)] = {"number": n, "title": f"Add helper {n}", "body": "", "state": "open",
                                        "labels": [{"name": "relay"}], "user": {"login": "alice"}, "assignees": []}
            db["next"] = 4
        path = self.root / "github.json"
        data = self.db()
        edit(data)
        path.write_text(json.dumps(data))
        talks = [Conversation(repo, config.load(repo), Store(repo), quiet) for repo in (self.a, self.b)]
        barrier = threading.Barrier(2)

        def sync(talk):
            barrier.wait()
            return talk.sync("main")

        with concurrent.futures.ThreadPoolExecutor() as pool:
            list(pool.map(sync, talks))
        found = self.store.read()[1]["tasks"]
        self.assertEqual(sorted(t["issue"] for t in found.values()), [1, 2, 3], "one task per issue")
        acks = collections.Counter(c["issue_url"] for c in self.db()["comments"])
        self.assertTrue(all(count == 1 for count in acks.values()), f"duplicate acknowledgement: {acks}")

    def test_cancel_racing_a_finishing_agent_leaves_one_consistent_outcome(self):
        cfg = config.load(self.a)
        task_id = tasks.add(self.store, "Implement answer() returning 42", self.base, base_branch="main",
                            created_by=cfg["worker"], by="alice", requested_by="alice")
        Conversation(self.a, config.load(self.a), self.store, quiet).flush(self.store.read()[1]["tasks"][task_id])
        worker = mock(self.b, "finish-fresh", emit=quiet, sync=0.1, heartbeat=0.2, ttl=30)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(worker.run, True)
            deadline = time.monotonic() + 20
            while self.store.read()[1]["tasks"][task_id]["status"] != "running":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            # The agent finishes immediately, so the cancel lands somewhere between claim and PR.
            with (self.root / "github.json").open("a+") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.seek(0)
                db = json.loads(handle.read())
                db["comments"].append({"id": 99, "body": "/relay cancel", "user": {"login": "alice"},
                                       "created_at": "2999-01-01T00:00:00Z", "updated_at": "2999-01-01T00:00:00Z",
                                       "issue_url": "https://api.github.com/repos/demo/shared/issues/1"})
                handle.seek(0)
                handle.truncate()
                json.dump(db, handle)
            Conversation(self.a, config.load(self.a), Store(self.a), quiet).sync("main")
            future.result(timeout=120)
        task = self.store.read()[1]["tasks"][task_id]
        pulls = self.db()["pulls"]
        # Cancelling a task that already reached review leaves its recorded PR for a person to
        # close or merge, so a cancelled task may own one open PR. What must never happen is an
        # open PR the task does not record (an orphan) or more than one.
        open_pulls = {int(n) for n, p in pulls.items() if p["state"] == "open"}
        self.assertLessEqual(open_pulls, {task["pr"]} if task.get("pr") else set(), f"orphan PR {open_pulls}")
        if task["status"] == "review":
            self.assertEqual(open_pulls, {task["pr"]})
        else:
            self.assertEqual(task["status"], "cancelled")

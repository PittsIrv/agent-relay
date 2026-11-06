import concurrent.futures
import sys
import io
import json
import tempfile
import time
import unittest
from pathlib import Path

from agent_relay import config, interactive, tasks
from agent_relay.conversation import Conversation
from agent_relay.demo import mock, setup
from agent_relay.gitstore import Store, output, private_dir

quiet = lambda _: None


class GitHubConversationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.a, self.b, self.base = setup(self.root, github=True)
        self.store = Store(self.a)

    def db(self):
        return json.loads((self.root / "github.json").read_text())

    def edit_db(self, change):
        db = self.db()
        change(db)
        (self.root / "github.json").write_text(json.dumps(db))

    def talk(self, repo):
        return Conversation(repo, config.load(repo), Store(repo), quiet)

    def add(self, prompt="Implement answer() returning 42", **kwargs):
        cfg = config.load(self.a)
        task_id = tasks.add(self.store, prompt, self.base, base_branch="main", created_by=cfg["worker"],
                            by="alice", requested_by="alice", **kwargs)
        return self.talk(self.a).flush(self.store.read()[1]["tasks"][task_id])

    def comment(self, number, body, login):
        def change(db):
            db["comments"].append({"id": len(db["comments"]) + 1, "body": body, "user": {"login": login},
                                   "created_at": "2999-01-01T00:00:00Z", "updated_at": "2999-01-01T00:00:00Z",
                                   "issue_url": f"https://api.github.com/repos/demo/shared/issues/{number}"})
        self.edit_db(change)

    def test_handoff_is_narrated_once_on_issue_and_pr(self):
        task = self.add()
        self.assertEqual(task["issue"], 1)
        mock(self.a, "limit", emit=quiet).run(once=True)
        mock(self.b, "finish", emit=quiet).run(once=True)
        task = self.store.read()[1]["tasks"][task["id"]]
        self.assertEqual(task["status"], "review")
        pull = self.db()["pulls"][str(task["pr"])]
        self.assertTrue(pull["draft"])
        self.assertEqual((pull["head"], pull["base"]), (task["checkpoint"]["branch"], "main"))
        self.assertIn("Closes #1", pull["body"])
        bodies = [c["body"] for c in self.db()["comments"]]
        kinds = ["picked this up", "usage limit", "picked this up", "finished attempt 2"]
        self.assertEqual(len(bodies), len(kinds))
        for body, expected in zip(bodies, kinds):
            self.assertIn(expected, body)
        self.assertNotIn("@", "".join(bodies).replace("<!--", ""))
        for repo in (self.a, self.b):
            self.talk(repo).flush_all(self.store.read()[1])
        self.assertEqual(len(self.db()["comments"]), len(kinds), "re-flushing must not duplicate comments")

    def test_issue_import_requires_write_access_and_is_idempotent(self):
        def file(db):
            for number, login in ((10, "mallory"), (11, "alice")):
                db["issues"][str(number)] = {"number": number, "title": f"Issue by {login}", "body": "Do it",
                                             "state": "open", "labels": [{"name": "relay"}],
                                             "user": {"login": login}}
            db["permissions"]["mallory"] = "read"
        self.edit_db(file)
        first = self.talk(self.b).sync("main")
        second = self.talk(self.a).sync("main")
        self.assertEqual(first["imported"], [11])
        self.assertEqual(second["imported"], [])
        state = self.store.read()[1]
        self.assertEqual([t["issue"] for t in state["tasks"].values()], [11])
        task = next(iter(state["tasks"].values()))
        self.assertEqual(task["prompt"], "Issue by alice\n\nDo it")
        self.assertIn("queued this issue", self.db()["comments"][-1]["body"])

    def test_comment_commands_from_writers_apply_exactly_once(self):
        task = self.add()
        mock(self.a, "error", emit=quiet).run(once=True)
        self.assertEqual(self.store.read()[1]["tasks"][task["id"]]["status"], "blocked")
        self.comment(1, "/relay retry the import path is wrong", "mallory")
        self.comment(1, "/relay retry use answer.py at the repo root", "bob")
        self.talk(self.a).sync("main")
        self.talk(self.b).sync("main")
        task = self.store.read()[1]["tasks"][task["id"]]
        self.assertEqual(task["status"], "queued")
        self.assertEqual([n["text"] for n in task["notes"]], ["use answer.py at the repo root"])
        self.assertEqual(len(self.db()["reactions"]), 1)
        self.assertIn("use answer.py at the repo root", interactive_prompt(task))

    def test_cancel_comment_stops_a_running_agent(self):
        task = self.add()
        worker = mock(self.a, "wait", emit=quiet)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(worker.run, True)
            deadline = time.monotonic() + 10
            while self.store.read()[1]["tasks"][task["id"]]["status"] != "running":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            self.comment(1, "/relay cancel", "alice")
            self.talk(self.b).sync("main")
            worker.heartbeat = 0.1
            future.result(timeout=20)
        self.assertEqual(self.store.read()[1]["tasks"][task["id"]]["status"], "cancelled")
        self.assertTrue(list(private_dir(self.a).glob("attempts/*/recovery.json")))

    def test_merged_pr_marks_done_and_unblocks_dependents(self):
        first = self.add()
        second = self.add("Use answer() in main.py", after=[first["id"]])
        self.assertIsNone(tasks.claim(Store(self.b), "probe", task_id=second["id"]))
        mock(self.b, "finish-fresh", emit=quiet).run(once=True)
        task = self.store.read()[1]["tasks"][first["id"]]
        self.assertEqual(task["status"], "review")
        self.edit_db(lambda db: db["pulls"][str(task["pr"])].update(merged=True))
        self.assertEqual(self.talk(self.a).sync("main")["done"], [first["id"]])
        self.assertEqual(tasks.claim(Store(self.b), "probe")["id"], second["id"])

    def test_assigned_issue_goes_only_to_that_owners_agent(self):
        self.edit_db(lambda db: db["issues"].update({"7": {
            "number": 7, "title": "Bob, please add median()", "body": "", "state": "open",
            "labels": [{"name": "relay"}], "user": {"login": "alice"}, "assignees": [{"login": "bob"}]}}))
        self.talk(self.a).sync("main")
        task = next(iter(self.store.read()[1]["tasks"].values()))
        self.assertEqual((task["for"], task["requested_by"]), (["bob"], "alice"))
        self.assertIn("Requested from **bob**'s agent", self.db()["comments"][-1]["body"])
        mock(self.a, "finish-fresh", emit=quiet).run(once=True)
        self.assertEqual(self.store.read()[1]["tasks"][task["id"]]["epoch"], 0, "alice's agent must not take bob's request")
        mock(self.b, "finish-fresh", emit=quiet).run(once=True)
        self.assertEqual(self.store.read()[1]["tasks"][task["id"]]["status"], "review")

    def test_owner_consent_policy(self):
        from_alice = self.add()
        config.update(self.b, accept_from=["carol"])
        mock(self.b, "finish-fresh", emit=quiet).run(once=True)
        self.assertEqual(self.store.read()[1]["tasks"][from_alice["id"]]["epoch"], 0)
        config.update(self.b, accept_from=["alice"], max_tasks_per_day=1)
        mock(self.b, "finish-fresh", emit=quiet).run(once=True)
        self.assertEqual(self.store.read()[1]["tasks"][from_alice["id"]]["status"], "review")
        second = self.add("Another task")
        mock(self.b, "finish-fresh", emit=quiet).run(once=True)
        self.assertEqual(self.store.read()[1]["tasks"][second["id"]]["epoch"], 0, "daily cap reached")
        presence = self.store.read()[1]["workers"][config.load(self.b)["worker"]]
        self.assertEqual(presence["accepts"], "alice; up to 1 tasks/day")

    def test_retry_after_review_gets_a_new_pr_and_old_merge_does_not_finish_it(self):
        task = self.add()
        mock(self.b, "finish-fresh", emit=quiet).run(once=True)
        first_pr = self.store.read()[1]["tasks"][task["id"]]["pr"]
        tasks.retry(self.store, task["id"], note="also handle negatives")
        self.edit_db(lambda db: db["pulls"][str(first_pr)].update(merged=True))
        self.assertEqual(self.talk(self.a).sync("main")["done"], [], "an old attempt's PR cannot complete the task")
        mock(self.b, "finish-fresh", emit=quiet).run(once=True)
        task = self.store.read()[1]["tasks"][task["id"]]
        self.assertNotEqual(task["pr"], first_pr)
        self.assertEqual(task["past_prs"][0]["pr"], first_pr)
        self.assertEqual(self.db()["pulls"][str(task["pr"])]["head"], task["checkpoint"]["branch"])
        self.edit_db(lambda db: db["pulls"][str(task["pr"])].update(merged=True))
        self.assertEqual(self.talk(self.a).sync("main")["done"], [task["id"]])

    def test_stale_pr_creation_cannot_attach_to_a_newer_attempt(self):
        task = self.add()
        good_gh = config.load(self.b)["gh"]
        config.update(self.b, gh=["false"])  # Bob's PR creation is "in flight" (fails for now).
        mock(self.b, "finish-fresh", emit=quiet).run(once=True)
        stale = self.store.read()[1]["tasks"][task["id"]]
        self.assertEqual((stale["status"], stale["pr"]), ("review", None))
        tasks.retry(self.store, task["id"], worker=config.load(self.a)["worker"], by="alice")
        mock(self.a, "finish-fresh", emit=quiet).run(once=True)
        live = self.store.read()[1]["tasks"][task["id"]]
        self.assertEqual(live["epoch"], 2)
        config.update(self.b, gh=good_gh)
        self.talk(self.b)._open_pr(stale, self.store.read()[1])  # Bob's stale request finally lands.
        after = self.store.read()[1]["tasks"][task["id"]]
        self.assertEqual(after["pr"], live["pr"])
        late = [p for p in self.db()["pulls"].values() if p["head"] == stale["checkpoint"]["branch"]]
        self.assertEqual([p["state"] for p in late], ["closed"])
        self.edit_db(lambda db: db["pulls"][str(late[0]["number"])].update(merged=True))
        self.assertEqual(self.talk(self.a).sync("main")["done"], [])

    def test_retry_closes_the_superseded_pr(self):
        task = self.add()
        mock(self.b, "finish-fresh", emit=quiet).run(once=True)
        old = self.store.read()[1]["tasks"][task["id"]]["pr"]
        tasks.retry(self.store, task["id"], worker=config.load(self.a)["worker"], by="alice")
        self.talk(self.a).flush(self.store.read()[1]["tasks"][task["id"]])
        self.assertEqual(self.db()["pulls"][str(old)]["state"], "closed")

    def test_slow_github_never_costs_the_lease(self):
        task = self.add()
        slow = [sys.executable, "-c", "import time; time.sleep(4)"]  # Each GitHub call outlasts the lease.
        config.update(self.b, gh=slow)
        worker = mock(self.b, "slow-finish", emit=quiet, ttl=3, heartbeat=1, sync=0.1)
        worker.run(once=True)
        self.assertEqual(self.store.read()[1]["tasks"][task["id"]]["status"], "review")

    def test_orphaned_issue_is_adopted_not_duplicated(self):
        cfg = config.load(self.a)
        task_id = tasks.add(self.store, "Orphan", self.base, base_branch="main", created_by=cfg["worker"])
        talk = self.talk(self.a)
        task = self.store.read()[1]["tasks"][task_id]
        talk.gh.create_issue("Orphan", talk.issue_body(task))  # Created, then the client crashed.
        task = talk.flush(task)
        self.assertEqual(task["issue"], 1)
        self.assertEqual(len(self.db()["issues"]), 1)

    def test_relay_owned_issue_is_never_imported_as_a_second_task(self):
        task = self.add()
        def unlink(db):  # Simulate the window between creating the issue and recording its number.
            db["issues"]["1"]["user"]["login"] = "alice"
        self.edit_db(unlink)
        state_tasks = self.store.read()[1]["tasks"]
        def drop_link(state):
            state["tasks"][task["id"]]["issue"] = None
            return True
        self.store.change(drop_link)
        self.assertEqual(self.talk(self.b).sync("main")["imported"], [])
        self.assertEqual(len(self.store.read()[1]["tasks"]), len(state_tasks))

    def test_cancel_reaches_a_running_agent_without_any_other_worker(self):
        task = self.add()
        worker = mock(self.a, "wait", emit=quiet, sync=0.2, heartbeat=0.2)
        with concurrent.futures.ThreadPoolExecutor() as pool:
            future = pool.submit(worker.run, True)
            deadline = time.monotonic() + 10
            while self.store.read()[1]["tasks"][task["id"]]["status"] != "running":
                self.assertLess(time.monotonic(), deadline)
                time.sleep(0.05)
            self.comment(1, "/relay cancel", "bob")
            future.result(timeout=20)
        self.assertEqual(self.store.read()[1]["tasks"][task["id"]]["status"], "cancelled")

    def test_commands_beyond_the_first_page_are_applied(self):
        task = self.add()
        mock(self.a, "error", emit=quiet).run(once=True)
        def chatter(db):
            for i in range(130):
                db["comments"].append({"id": 1000 + i, "body": f"thinking out loud {i}", "user": {"login": "bob"},
                                       "created_at": "2999-01-01T00:00:00Z", "updated_at": "2999-01-01T00:00:00Z",
                                       "issue_url": "https://api.github.com/repos/demo/shared/issues/1"})
        self.edit_db(chatter)
        self.comment(1, "/relay retry fix the import", "bob")
        self.talk(self.a).sync("main")
        self.assertEqual(self.store.read()[1]["tasks"][task["id"]]["status"], "queued")

    def test_revoked_write_access_stops_working(self):
        from agent_relay import github
        task = self.add()
        mock(self.a, "error", emit=quiet).run(once=True)
        talk = self.talk(self.a)
        self.assertTrue(talk.gh.can_write("bob"))
        self.edit_db(lambda db: db["permissions"].update(bob="read"))
        original = github.PERMISSION_TTL
        github.PERMISSION_TTL = 0
        try:
            self.comment(1, "/relay retry", "bob")
            talk.sync("main")
        finally:
            github.PERMISSION_TTL = original
        self.assertEqual(self.store.read()[1]["tasks"][task["id"]]["status"], "blocked")

    def test_lost_local_cache_does_not_duplicate_comments(self):
        self.add()
        mock(self.a, "limit", emit=quiet).run(once=True)
        before = len(self.db()["comments"])
        (private_dir(self.a) / "posted.json").unlink()
        self.talk(self.a).flush_all(self.store.read()[1])
        self.assertEqual(len(self.db()["comments"]), before)

    def test_github_timeout_is_contained(self):
        config.update(self.a, gh=[sys.executable, "-c", "import time; time.sleep(5)"])
        talk = self.talk(self.a)
        talk.gh.timeout = 0.2
        with self.assertRaisesRegex(Exception, "timed out"):
            talk.gh.login()
        task_id = tasks.add(self.store, "Slow GitHub", self.base, base_branch="main",
                            created_by=config.load(self.a)["worker"])
        task = talk.flush(self.store.read()[1]["tasks"][task_id])
        self.assertIsNone(task["issue"])

    def test_github_outage_never_blocks_a_handoff(self):
        cfg = config.load(self.a)
        config.update(self.a, gh=["false"])
        task = self.add()
        self.assertIsNone(task["issue"])
        mock(self.a, "limit", emit=quiet).run(once=True)
        self.assertEqual(self.store.read()[1]["tasks"][task["id"]]["status"], "queued")
        config.update(self.a, gh=cfg["gh"])
        self.talk(self.a).flush_all(self.store.read()[1])
        task = self.store.read()[1]["tasks"][task["id"]]
        self.assertEqual(task["issue"], 1)
        self.assertEqual(len(self.db()["comments"]), 2, "deferred claim and handoff comments are posted later")


    def failing_take(self, repo, task_id):
        from unittest.mock import patch
        real = interactive.git
        def flaky(where, *args, **kwargs):
            if args and args[0] == "switch":
                raise interactive.RelayError("simulated checkout failure")
            return real(where, *args, **kwargs)
        with patch("agent_relay.interactive.git", side_effect=flaky):
            with self.assertRaisesRegex(Exception, "checkout failure"):
                interactive.take(repo, config.load(repo), Store(repo), task_id, emit=quiet)

    def test_failed_take_of_a_reviewed_task_keeps_its_pr_live(self):
        task = self.add()
        mock(self.b, "finish-fresh", emit=quiet).run(once=True)
        pr = self.store.read()[1]["tasks"][task["id"]]["pr"]
        self.failing_take(self.a, task["id"])
        after = self.store.read()[1]["tasks"][task["id"]]
        self.assertEqual((after["status"], after["pr"], after["pr_epoch"]), ("review", pr, after["epoch"]))
        self.talk(self.a).flush(after)  # The rolled-back hold must not close the restored PR.
        self.assertEqual(self.db()["pulls"][str(pr)]["state"], "open")
        self.edit_db(lambda db: db["pulls"][str(pr)].update(merged=True))
        self.assertEqual(self.talk(self.a).sync("main")["done"], [task["id"]])

    def test_failed_take_of_an_expired_running_task_requeues_it(self):
        task = self.add()
        tasks.claim(self.store, "gone", ttl=1, now=time.time() - 60)
        self.failing_take(self.a, task["id"])
        after = self.store.read()[1]["tasks"][task["id"]]
        self.assertEqual((after["status"], after["lease"]), ("queued", None))
        self.assertEqual(tasks.claim(self.store, "pool")["id"], task["id"])

    def test_pr_attachment_is_idempotent(self):
        task = self.add()
        mock(self.b, "finish-fresh", emit=quiet).run(once=True)
        live = self.store.read()[1]["tasks"][task["id"]]
        self.assertTrue(tasks.attach_pr(self.store, task["id"], live["epoch"], live["pr"], True))
        self.talk(self.b)._open_pr(live, self.store.read()[1])  # A repeated completion adopts, never closes.
        self.assertEqual(self.db()["pulls"][str(live["pr"])]["state"], "open")
        self.assertEqual(self.store.read()[1]["tasks"][task["id"]]["pr"], live["pr"])

    def test_interrupted_close_of_a_stale_pr_is_retried(self):
        task = self.add()
        good_gh = config.load(self.b)["gh"]
        config.update(self.b, gh=["false"])
        mock(self.b, "finish-fresh", emit=quiet).run(once=True)
        stale = self.store.read()[1]["tasks"][task["id"]]
        tasks.retry(self.store, task["id"], worker=config.load(self.a)["worker"], by="alice")
        mock(self.a, "finish-fresh", emit=quiet).run(once=True)
        config.update(self.b, gh=good_gh)
        talk = self.talk(self.b)
        def outage(number):
            raise interactive.RelayError("GitHub is down")
        talk.gh.close_pr = outage
        talk.flush(stale)  # Bob's late PR is refused and recorded, but the close fails.
        late = [p for p in self.db()["pulls"].values() if p["head"] == stale["checkpoint"]["branch"]]
        self.assertEqual([p["state"] for p in late], ["open"])
        self.talk(self.b).flush(self.store.read()[1]["tasks"][task["id"]])  # GitHub is back.
        late = [p for p in self.db()["pulls"].values() if p["head"] == stale["checkpoint"]["branch"]]
        self.assertEqual([p["state"] for p in late], ["closed"])


class InteractiveHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.a, self.b, self.base = setup(self.temp.name)
        self.store = Store(self.a)

    def hook(self, error="rate_limit"):
        payload = {"hook_event_name": "StopFailure", "error": error, "cwd": str(self.a)}
        return interactive.run_hook(stdin=io.StringIO(json.dumps(payload)), emit=quiet)

    def tasks(self):
        return self.store.read()[1]["tasks"]

    def test_rate_limit_hook_publishes_uncommitted_work_without_touching_checkout(self):
        (self.a / "answer.py").write_text("def answer():\n    return None\n")
        (self.a / "README.md").write_text("edited\n")
        before = (output(self.a, "status", "--porcelain"), output(self.a, "rev-parse", "HEAD"))
        task = self.hook()
        self.assertEqual(task["status"], "queued")
        self.assertEqual((output(self.a, "status", "--porcelain"), output(self.a, "rev-parse", "HEAD")), before)
        self.assertEqual(sorted(task["checkpoint"]["summary"]["files"]), ["README.md", "answer.py"])
        self.assertIn("answer.py", task["prompt"])
        mock(self.b, "finish", emit=quiet).run(once=True)
        final = self.tasks()[task["id"]]
        self.assertEqual((final["status"], final["verification"]["result"]), ("review", "passed"))

    def test_other_errors_and_clean_trees_do_nothing(self):
        (self.a / "answer.py").write_text("x = 1\n")
        self.assertIsNone(self.hook("overloaded"))
        (self.a / "answer.py").unlink()
        self.assertIsNone(self.hook())
        self.assertEqual(self.tasks(), {})

    def test_repeated_hook_reuses_the_same_task(self):
        (self.a / "answer.py").write_text("def answer():\n    return None\n")
        first = self.hook()
        again = self.hook()
        self.assertEqual(again["id"], first["id"])
        (self.a / "notes.txt").write_text("more\n")
        updated = self.hook()
        self.assertEqual(updated["id"], first["id"])
        self.assertEqual(len(self.tasks()), 1)
        self.assertIn("notes.txt", updated["checkpoint"]["summary"]["files"])

    def test_sensitive_files_block_the_hook(self):
        (self.a / ".env").write_text("TOKEN=secret\n")
        self.assertIsNone(self.hook())
        self.assertEqual(self.tasks(), {})
        self.assertIn("sensitive filename", (private_dir(self.a) / "hook.log").read_text())

    def test_take_holds_task_and_handoff_returns_it(self):
        (self.a / "answer.py").write_text("def answer():\n    return None\n")
        task = self.hook()
        cfg_b = config.load(self.b)
        held = interactive.take(self.b, cfg_b, Store(self.b), task["id"], emit=quiet)
        self.assertEqual(held["status"], "held")
        self.assertEqual(output(self.b, "symbolic-ref", "--short", "HEAD"), f"relay/{task['id']}")
        self.assertIn("return None", (self.b / "answer.py").read_text())
        self.assertIsNone(tasks.claim(self.store, "pool", now=time.time() + 30 * 86400))
        (self.b / "answer.py").write_text("def answer():\n    return 41  # nearly\n")
        back = interactive.handoff(self.b, config.load(self.b), Store(self.b), message="off by one left", emit=quiet)
        self.assertEqual((back["id"], back["status"], back["epoch"]), (task["id"], "queued", 2))
        self.assertIn("return 41", output(self.b, "show", f"{back['checkpoint']['sha']}:answer.py"))
        self.assertEqual(back["notes"][-1]["text"], "off by one left")
        self.assertEqual(config.load(self.b)["held"], {})

    def test_failed_take_returns_the_task_to_the_queue(self):
        from unittest.mock import patch
        (self.a / "answer.py").write_text("def answer():\n    return None\n")
        task = self.hook()
        real = interactive.git
        def flaky(repo, *args, **kwargs):
            if args and args[0] == "switch":
                raise interactive.RelayError("simulated checkout failure")
            return real(repo, *args, **kwargs)
        with patch("agent_relay.interactive.git", side_effect=flaky):
            with self.assertRaisesRegex(Exception, "checkout failure"):
                interactive.take(self.b, config.load(self.b), Store(self.b), task["id"], emit=quiet)
        self.assertEqual(self.tasks()[task["id"]]["status"], "queued")
        mock(self.b, "error", emit=quiet).run(once=True)
        self.assertEqual(self.tasks()[task["id"]]["status"], "blocked")
        with patch("agent_relay.interactive.git", side_effect=flaky):
            with self.assertRaisesRegex(Exception, "checkout failure"):
                interactive.take(self.b, config.load(self.b), Store(self.b), task["id"], emit=quiet)
        self.assertEqual(self.tasks()[task["id"]]["status"], "blocked", "a failed take restores, never requeues")

    def test_hook_publishes_new_work_after_the_previous_task_moved_on(self):
        (self.a / "answer.py").write_text("def answer():\n    return None\n")
        first = self.hook()
        mock(self.b, "finish", emit=quiet).run(once=True)
        self.assertEqual(self.hook()["id"], first["id"], "same tree: nothing new")
        (self.a / "other.py").write_text("x = 1\n")
        second = self.hook()
        self.assertNotEqual(second["id"], first["id"])
        self.assertEqual(len(self.tasks()), 2)

    def test_take_refuses_dirty_checkout(self):
        (self.a / "answer.py").write_text("def answer():\n    return None\n")
        task = self.hook()
        (self.b / "scratch.txt").write_text("mine\n")
        with self.assertRaisesRegex(Exception, "Commit or stash"):
            interactive.take(self.b, config.load(self.b), Store(self.b), task["id"], emit=quiet)

    def test_hook_install_merges_and_uninstalls(self):
        settings = self.a / ".claude" / "settings.local.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"permissions": {"allow": ["Bash(ls)"]}}))
        interactive.install_hook(self.a)
        interactive.install_hook(self.a)
        data = json.loads(settings.read_text())
        self.assertEqual(data["permissions"], {"allow": ["Bash(ls)"]})
        self.assertEqual(len(data["hooks"]["StopFailure"]), 1)
        self.assertEqual(data["hooks"]["StopFailure"][0]["matcher"], "rate_limit")
        self.assertEqual(output(self.a, "status", "--porcelain"), "")
        interactive.install_hook(self.a, uninstall=True)
        self.assertNotIn("StopFailure", json.loads(settings.read_text())["hooks"])

    def test_presence_is_reported(self):
        mock(self.a, "limit", emit=quiet).run(once=True)
        workers = self.store.read()[1]["workers"]
        me = config.load(self.a)["worker"]
        self.assertEqual(workers[me]["mode"], "offline")
        self.assertIn("simulated agent", workers[me]["label"])


def interactive_prompt(task):
    from agent_relay.adapters import prompt
    return prompt(task)


if __name__ == "__main__":
    unittest.main()

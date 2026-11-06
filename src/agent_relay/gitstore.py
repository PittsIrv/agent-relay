"""Optimistic Git transactions; no working-tree writes and no external service."""

import json
import os
import random
import subprocess
import threading
import time
from pathlib import Path

STATE_REF = "refs/heads/relay/state"


class RelayError(Exception):
    pass


def git(repo, *args, input=None, env=None, check=True):
    result = subprocess.run(
        ["git", "-C", str(repo), *args], input=input, capture_output=True,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}, timeout=45,
    )
    if check and result.returncode:
        raise RelayError(result.stderr.decode(errors="replace").strip() or "Git operation failed")
    return result


def output(repo, *args, **kwargs):
    return git(repo, *args, **kwargs).stdout.decode().strip()


def repository(path="."):
    return Path(output(path, "rev-parse", "--show-toplevel"))


def private_dir(repo):
    return Path(output(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "agent-relay"


def empty_state():
    return {"version": 1, "tasks": {}}


class Store:
    def __init__(self, repo, remote="origin"):
        self.repo = Path(repo)
        self.remote = remote
        if remote.startswith("-"):
            raise RelayError("Remote must be a configured Git remote name")
        if remote not in output(repo, "remote").splitlines():
            raise RelayError(f"Git remote {remote!r} is not configured")

    def read(self):
        refs = output(self.repo, "ls-remote", "--refs", self.remote, STATE_REF)
        if not refs:
            return None, empty_state()
        sha = refs.split()[0]
        # Fetch the exact object, not FETCH_HEAD (which other processes can change).
        git(self.repo, "fetch", "--quiet", "--no-tags", self.remote, sha)
        try:
            state = json.loads(output(self.repo, "show", f"{sha}:state.json"))
        except (ValueError, KeyError) as exc:
            raise RelayError("Invalid relay/state metadata") from exc
        if state.get("version") != 1 or not isinstance(state.get("tasks"), dict):
            raise RelayError("Unsupported relay/state format")
        return sha, state

    def commit(self, parent, state):
        data = (json.dumps(state, sort_keys=True, indent=2) + "\n").encode()
        blob = output(self.repo, "hash-object", "-w", "--stdin", input=data)
        tree = output(self.repo, "mktree", input=f"100644 blob {blob}\tstate.json\n".encode())
        args = ["commit-tree", tree, "-m", "relay: update task state"]
        if parent:
            args += ["-p", parent]
        sha = output(self.repo, *args)
        result = git(
            self.repo, "push", "--quiet", f"--force-with-lease={STATE_REF}:{parent or ''}",
            self.remote, f"{sha}:{STATE_REF}", check=False,
        )
        if result.returncode:
            # Only retry actual contention, not auth, branch protection, or outages.
            latest = output(self.repo, "ls-remote", "--refs", self.remote, STATE_REF)
            latest_sha = latest.split()[0] if latest else None
            if latest_sha == sha:
                return True  # Pushed; only a local bookkeeping step failed. Never apply twice.
            if latest_sha != parent:
                return False
            raise RelayError(result.stderr.decode(errors="replace").strip())
        return True

    _locks = {}
    _guard = threading.Lock()

    def change(self, mutate):
        """mutate may run again on contention; it must have no external side effects.
        Threads of one process (heartbeat and GitHub sync) take turns per clone."""
        with Store._guard:
            lock = Store._locks.setdefault(str(self.repo.resolve()), threading.Lock())
        with lock:
            return self._change(mutate)

    def _change(self, mutate):
        for attempt in range(10):
            parent, state = self.read()
            result = mutate(state)
            if result is None:
                return None
            if self.commit(parent, state):
                return result
            time.sleep(random.uniform(0.02, 0.08) * (attempt + 1))
        raise RelayError("State is busy; retry shortly")

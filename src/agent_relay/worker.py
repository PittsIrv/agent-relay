import fcntl
import json
import os
import signal
import subprocess
import time
from pathlib import Path

from . import adapters, config, tasks
from .checkpoint import publish, snapshot
from .gitstore import RelayError, Store, git, private_dir


def signal_group(proc, sig):
    if proc is not None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass


def stop_group(proc):
    if proc is None:
        return
    signal_group(proc, signal.SIGCONT)
    signal_group(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass
    # Also terminate remaining tools if the CLI exited before its children did.
    signal_group(proc, signal.SIGKILL)
    proc.wait()


class Worker:
    def __init__(self, repo, agent="codex", *, argv=None, heartbeat=60, interval=120,
                 ttl=300, poll=2, cooldown=1800, max_runtime=3600, emit=print):
        self.repo = Path(repo)
        self.cfg = config.load(repo)
        self.store = Store(repo, self.cfg["remote"])
        self.argv = argv or adapters.command(agent)
        self.worker = self.cfg["worker"]
        self.folder = private_dir(repo)
        self.heartbeat = heartbeat
        self.interval = interval
        self.ttl = ttl
        self.poll = poll
        self.cooldown = cooldown
        self.max_runtime = max_runtime
        self.emit = emit
        self.stopping = False
        if ttl < heartbeat * 3:
            raise RelayError("Lease duration must be at least three heartbeat intervals")

    def run(self, once=False, opt_in=False):
        with (self.folder / "worker.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RelayError("A worker is already running in this clone") from exc
            if opt_in:
                config.update(self.repo, mode="pooled")
            while not self.stopping:
                cfg = config.load(self.repo)
                cooling = cfg.get("cooldown_until", 0) > time.time()
                if cfg["mode"] != "pooled" or cooling:
                    if once:
                        self.emit("Worker is paused or cooling down; no task claimed.")
                        return
                    time.sleep(self.poll)
                    continue
                task = tasks.claim(self.store, self.worker, self.ttl)
                if task:
                    self.attempt(task)
                if once:
                    if not task:
                        self.emit("No queued task.")
                    return
                time.sleep(self.poll)

    def attempt(self, task):
        task_id, epoch = task["id"], task["epoch"]
        attempt_dir = self.folder / "attempts" / f"{task_id}-{epoch}"
        attempt_dir.mkdir(parents=True, exist_ok=False)
        worktree = attempt_dir / "worktree"
        (attempt_dir / "task.json").write_text(json.dumps(task, indent=2))
        parent = (task.get("checkpoint") or {}).get("sha") or task["base"]
        proc = None
        log_path = attempt_dir / "agent.log"
        self.emit(f"Claimed {task_id} (attempt {epoch}); local files: {attempt_dir}")

        def renew(**kwargs):
            return tasks.update(self.store, task_id, self.worker, epoch, ttl=self.ttl, **kwargs)

        def checkpoint():
            nonlocal parent
            # Freeze the process group; partial application writes remain unvalidated.
            signal_group(proc, signal.SIGSTOP)
            try:
                renew()  # Check lease before creating/pushing this attempt's branch.
                sha = snapshot(worktree, parent, task_id, epoch)
                # Persist recovery coordinates before any network publication.
                (attempt_dir / "recovery.json").write_text(json.dumps({"sha": sha, "task": task_id, "epoch": epoch}))
                cp = publish(self.repo, self.cfg["remote"], sha, task_id, epoch)
                renew(checkpoint=cp)  # CAS fences stale publication to canonical state.
                parent = sha
                self.emit(f"Checkpoint {sha[:10]} published for {task_id}")
                return cp
            finally:
                signal_group(proc, signal.SIGCONT)

        try:
            git(self.repo, "fetch", "--quiet", "--no-tags", self.cfg["remote"], parent)
            git(self.repo, "worktree", "add", "--quiet", "--detach", str(worktree), parent)
            prompt_path = attempt_dir / "prompt.txt"
            prompt_path.write_text(adapters.prompt(task))
            with log_path.open("wb") as log, prompt_path.open("rb") as prompt_file:
                proc = subprocess.Popen(self.argv, cwd=worktree, stdin=prompt_file,
                                        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                started = last_renew = last_checkpoint = time.monotonic()
                interrupted = False
                while proc.poll() is None:
                    now = time.monotonic()
                    if self.stopping or config.load(self.repo)["mode"] != "pooled":
                        interrupted = True
                        stop_group(proc)
                        break
                    if now - started > self.max_runtime:
                        stop_group(proc)
                        raise RelayError("Session runtime limit reached; local work retained")
                    if now - last_renew >= self.heartbeat:
                        renew()
                        last_renew = time.monotonic()
                    if now - last_checkpoint >= self.interval:
                        checkpoint()
                        last_checkpoint = time.monotonic()
                    time.sleep(self.poll)
                returncode = proc.wait()
                stop_group(proc)
            checkpoint()
            if interrupted:
                renew(status="queued", reason="Owner paused or stopped worker")
                return
            # Bound memory; raw provider logs remain local and are never uploaded.
            with log_path.open("rb") as log:
                log.seek(max(0, log_path.stat().st_size - 2_000_000))
                kind = adapters.failure_kind(log.read().decode(errors="replace"), returncode)
            if kind == "quota":
                config.update(self.repo, cooldown_until=time.time() + self.cooldown)
                renew(status="queued", reason="Provider reported a usage/rate limit")
                self.emit(f"Handed off {task_id}; this worker will wait {self.cooldown}s before trying again.")
            elif kind == "error":
                renew(status="blocked", reason="Agent failed; inspect local agent.log and retry")
            else:
                checked_sha = parent
                verification = self.verify(worktree, attempt_dir, renew)
                verification["input_sha"] = checked_sha
                # Verification can modify files. Preserve them but don't call them verified.
                checkpoint()
                verification["snapshot_changed_during_check"] = parent != checked_sha
                renew(status="review", reason="Agent finished; human review required", verification=verification)
                self.emit(f"{task_id} ready for review; check: {verification['result']}. Nothing merged.")
        except (Exception, KeyboardInterrupt) as exc:
            stop_group(proc)
            # Preserve work locally; never advertise an unpushed snapshot as durable.
            if worktree.exists():
                try:
                    sha = snapshot(worktree, parent, task_id, epoch)
                    (attempt_dir / "recovery.json").write_text(json.dumps({"sha": sha, "task": task_id, "epoch": epoch}))
                except Exception:
                    pass
            try:
                renew(status="blocked", reason="Supervisor stopped; inspect the owning worker's local recovery files")
            except Exception:
                pass  # Lost lease/network: successor uses last advertised remote checkpoint.
            raise RelayError(f"{exc}. Recovery files retained in {attempt_dir}") from exc

    def verify(self, worktree, attempt_dir, renew):
        argv = self.cfg.get("check")
        if not argv:
            return {"result": "not configured"}
        proc = None
        try:
            with (attempt_dir / "check.log").open("wb") as log:
                proc = subprocess.Popen(argv, cwd=worktree, stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True)
                started = last_renew = time.monotonic()
                while proc.poll() is None:
                    if self.stopping or config.load(self.repo)["mode"] != "pooled":
                        return {"result": "interrupted"}
                    if time.monotonic() - started > 600:
                        return {"result": "timed out"}
                    if time.monotonic() - last_renew >= self.heartbeat:
                        renew()
                        last_renew = time.monotonic()
                    time.sleep(self.poll)
                return {"result": "passed" if proc.returncode == 0 else "failed", "exit_code": proc.returncode}
        finally:
            stop_group(proc)

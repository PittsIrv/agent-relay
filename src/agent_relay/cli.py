import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys

from . import __version__, config, tasks
from .gitstore import RelayError, Store, git, output, repository
from .worker import Worker


def parser():
    app = argparse.ArgumentParser(prog="relay", description="Pass unfinished coding tasks between collaborators' local agents.")
    app.add_argument("--version", action="version", version=__version__)
    sub = app.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init", help="Enroll this clone (starts paused; does not launch agents)")
    init.add_argument("--remote", default="origin")
    init.add_argument("--check", help="Local verification command, e.g. 'python -m unittest discover -s tests' (no shell)")
    add = sub.add_parser("add", help="Publish a task to the shared queue")
    add.add_argument("prompt")
    add.add_argument("--base", help="Remote branch to start from; defaults to the current branch")
    start = sub.add_parser("start", help="Opt in and run a worker in the foreground; Ctrl-C hands off")
    start.add_argument("--agent", choices=["codex", "claude"], default="codex")
    start.add_argument("--once", action="store_true", help="Attempt at most one task and exit")
    for name, default in [("checkpoint-seconds", 120), ("cooldown-seconds", 1800), ("max-runtime-seconds", 3600)]:
        start.add_argument(f"--{name}", type=int, default=default)
    sub.add_parser("pause", help="Reserve this clone's worker; checkpoint and release current work")
    sub.add_parser("resume", help="Allow an already-running paused worker to pick up tasks")
    status = sub.add_parser("status", help="Show shared tasks and this worker's mode")
    status.add_argument("--json", action="store_true")
    show = sub.add_parser("show", help="Inspect task events and its latest checkpoint")
    show.add_argument("task")
    retry = sub.add_parser("retry", help="Requeue a blocked or review task from its latest checkpoint")
    retry.add_argument("task")
    sub.add_parser("doctor", help="Check prerequisites without launching an agent")
    sub.add_parser("demo", help="Run an offline, two-clone handoff with fake agents; no model usage")
    return app


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == "demo":
            from .demo import run
            run()
            return
        if args.action == "doctor":
            print(f"Agent Relay {__version__} | Python {sys.version.split()[0]} | {sys.platform}")
            for program in ("git", "codex", "claude"):
                print(f"{program}: {shutil.which(program) or 'not installed'}")
            print("macOS/Linux supported. Authentication and real provider handoffs are not tested by doctor.")
            return
        repo = repository()
        if args.action == "init":
            Store(repo, args.remote)
            output(repo, "var", "GIT_AUTHOR_IDENT")
            check = shlex.split(args.check) if args.check else None
            cfg = config.initialize(repo, args.remote, check)
            print(f"Enrolled this clone as {cfg['worker']} ({cfg['mode']}).")
            print("Only tasks and checkpoint code are shared; logs and provider credentials stay local.")
            print("Next: relay add 'your task', then relay start --agent codex")
            return
        cfg = config.load(repo)
        store = Store(repo, cfg["remote"])
        if args.action in ("pause", "resume"):
            cfg = config.update(repo, mode="paused" if args.action == "pause" else "pooled")
            print(f"Worker {cfg['worker']}: {cfg['mode']} (applies to this clone only).")
        elif args.action == "add":
            base = args.base or output(repo, "symbolic-ref", "--short", "HEAD")
            if not git(repo, "check-ref-format", "--branch", base, check=False).returncode == 0:
                raise RelayError("Invalid base branch")
            refs = output(repo, "ls-remote", "--refs", cfg["remote"], f"refs/heads/{base}")
            if not refs:
                raise RelayError("Base branch is not on the remote; push it before adding a task")
            sha = refs.split()[0]
            task_id = tasks.add(store, args.prompt, sha)
            print(f"Queued {task_id} from {base}@{sha[:10]}. Task text is visible to repository collaborators.")
        elif args.action == "start":
            if os.name != "posix":
                raise RelayError("Workers currently require macOS or Linux")
            if not shutil.which(args.agent):
                raise RelayError(f"Install and sign in to {args.agent} first")
            if min(args.checkpoint_seconds, args.cooldown_seconds, args.max_runtime_seconds) < 10:
                raise RelayError("Timing options must be at least 10 seconds")
            worker = Worker(repo, args.agent, interval=args.checkpoint_seconds,
                            cooldown=args.cooldown_seconds, max_runtime=args.max_runtime_seconds)
            def stop(signum, frame):
                worker.stopping = True
            previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
            print(f"Worker {cfg['worker']} is available for this repository. Ctrl-C checkpoints and hands off.")
            try:
                worker.run(once=args.once, opt_in=True)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        elif args.action == "status":
            _, state = store.read()
            if args.json:
                print(json.dumps({"worker": cfg, "state": state}, indent=2))
                return
            print(f"This worker: {cfg['worker']} | mode: {cfg['mode']}")
            print("Mode is owner intent, not proof that a worker process is online.")
            if cfg.get("cooldown_until"):
                import time
                print(f"Local cooldown: {max(0, int(cfg['cooldown_until'] - time.time()))}s remaining")
            for task in state["tasks"].values():
                cp = (task.get("checkpoint") or {}).get("sha", "—")[:10]
                title = " ".join(task["prompt"].split())[:70]
                print(f"{task['id']}  {task['status']:8}  checkpoint {cp:10}  {title}")
            if not state["tasks"]:
                print("No tasks yet. Use: relay add 'your task'")
        elif args.action == "show":
            _, state = store.read()
            if args.task not in state["tasks"]:
                raise RelayError("Unknown task")
            task = state["tasks"][args.task]
            print(json.dumps(task, indent=2))
            cp = task.get("checkpoint")
            if cp:
                print(f"\nReview without changing your current branch:\n  git fetch {shlex.quote(cfg['remote'])} {shlex.quote(cp['branch'])}\n  git diff {task['base']} {cp['sha']}")
        elif args.action == "retry":
            print(f"Queued {tasks.retry(store, args.task)}")
    except (RelayError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"relay: {exc}", file=sys.stderr)
        sys.exit(1)

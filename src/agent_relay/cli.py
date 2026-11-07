import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time

from . import __version__, config, interactive, tasks
from .conversation import Conversation
from .github import detect
from .gitstore import RelayError, Store, git, output, repository
from .worker import Worker

ONLINE_SECONDS = 600


def parser():
    app = argparse.ArgumentParser(prog="relay", description="Hand unfinished coding work between collaborators' agents through GitHub.")
    app.add_argument("--version", action="version", version=__version__)
    sub = app.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init", help="Enroll this clone (starts paused; does not launch agents)")
    init.add_argument("--remote", default="origin")
    init.add_argument("--check", help="Local verification command, e.g. 'python -m unittest discover -s tests' (no shell)")
    init.add_argument("--no-github", action="store_true", help="Do not mirror tasks to GitHub issues and PRs")
    add = sub.add_parser("add", help="Publish a task to the shared queue")
    add.add_argument("prompt")
    add.add_argument("--base", help="Remote branch to start from; defaults to the current branch")
    add.add_argument("--after", action="append", default=[], metavar="TASK", help="Start only after TASK is merged (repeatable)")
    add.add_argument("--for", dest="for_", action="append", default=[], metavar="OWNER",
                     help="Ask a specific collaborator's agent (GitHub login); repeatable")
    start = sub.add_parser("start", help="Opt in and run a worker in the foreground; Ctrl-C hands off")
    start.add_argument("--agent", choices=["codex", "claude"], default="codex")
    start.add_argument("--model", help="Model name passed to the agent CLI")
    start.add_argument("--once", action="store_true", help="Attempt at most one task and exit")
    start.add_argument("--accept-from", metavar="LOGINS", help="Comma-separated collaborators whose tasks you accept (default: anyone with write access)")
    start.add_argument("--max-tasks-per-day", type=int, metavar="N", help="Stop accepting new tasks after N per local day")
    for name, default in [("checkpoint-seconds", 120), ("cooldown-seconds", 1800), ("max-runtime-seconds", 3600)]:
        start.add_argument(f"--{name}", type=int, default=default)
    handoff = sub.add_parser("handoff", help="Publish your own in-progress work so another agent can continue it")
    handoff.add_argument("message", nargs="?", help="What the next agent should do")
    handoff.add_argument("--review", action="store_true", help="Mark the work finished and open a PR instead")
    handoff.add_argument("--from-hook", action="store_true", help=argparse.SUPPRESS)
    take = sub.add_parser("take", help="Hold a task and check out its latest checkpoint for interactive work")
    take.add_argument("task")
    hook = sub.add_parser("hook", help="Install the Claude Code usage-limit hook in this clone")
    hook.add_argument("verb", choices=["install", "uninstall"])
    hook.add_argument("--share-prompt", action="store_true", help="Include your last prompt in the handoff task")
    sub.add_parser("pause", help="Reserve this clone's worker; checkpoint and release current work")
    sub.add_parser("resume", help="Allow an already-running paused worker to pick up tasks")
    status = sub.add_parser("status", help="Show who is online and every task")
    status.add_argument("--json", action="store_true")
    show = sub.add_parser("show", help="Show a task's timeline and its latest checkpoint")
    show.add_argument("task")
    show.add_argument("--json", action="store_true")
    note = sub.add_parser("note", help="Leave guidance that the next agent on a task will read")
    note.add_argument("task")
    note.add_argument("text")
    retry = sub.add_parser("retry", help="Requeue a blocked, review or cancelled task from its latest checkpoint")
    retry.add_argument("task")
    retry.add_argument("note", nargs="?")
    cancel = sub.add_parser("cancel", help="Cancel a task; a running agent stops at its next heartbeat, a reviewed task's PR is closed (owner only)")
    cancel.add_argument("task")
    sub.add_parser("sync", help="Import labelled issues, apply /relay comments, mark merged PRs done")
    sub.add_parser("doctor", help="Check prerequisites without launching an agent")
    sub.add_parser("demo", help="Run an offline, two-clone handoff with fake agents; no model usage")
    return app


def ago(seconds):
    seconds = max(0, int(time.time() - seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


def board(cfg, state):
    workers = state.get("workers", {})
    running = {t["lease"]["worker"]: t["id"] for t in state["tasks"].values()
               if t["status"] in ("running", "held") and t.get("lease")}
    print("WORKERS")
    if not workers:
        print("  none have announced themselves yet (relay start)")
    for worker_id, info in sorted(workers.items(), key=lambda kv: -kv[1].get("last_seen", 0)):
        online = info.get("mode") != "offline" and time.time() - info.get("last_seen", 0) < ONLINE_SECONDS
        doing = f"working on {running[worker_id]}" if worker_id in running else ("idle" if online else "")
        me = "  (this clone)" if worker_id == cfg["worker"] else ""
        state_word = info.get("mode", "?") if online else "offline"
        print(f"  {info.get('label', worker_id):28} {state_word:8} {doing:22} seen {ago(info.get('last_seen', 0))}{me}")
        if online and info.get("accepts"):
            print(f"  {'':28} accepts work from: {info['accepts']}")
    print("\nTASKS")
    if not state["tasks"]:
        print("  none yet: relay add 'your task'  (or label a GitHub issue 'relay')")
    for task in sorted(state["tasks"].values(), key=lambda t: t["created_at"]):
        cp = (task.get("checkpoint") or {}).get("sha", "")[:7] or "-"
        link = f"#{task['issue']}" if task.get("issue") else ""
        if task.get("pr"):
            link += f" PR#{task['pr']}"
        waits = f" (after {', '.join(task['after'])})" if task.get("after") and not tasks.ready(task, state) else ""
        if task.get("for") and task["status"] == "queued":
            waits += f" (for {', '.join(task['for'])})"
        title = " ".join(task["prompt"].split())[:52]
        print(f"  {task['id']}  {task['status']:9} {cp:8} {link:13} {title}{waits}")


def timeline(task):
    for ev in task["events"]:
        stamp = time.strftime("%m-%d %H:%M", time.localtime(ev["at"]))
        who = ev.get("by") or ev.get("worker") or ""
        detail = ev.get("reason") or ev.get("note") or (ev.get("sha", "")[:10] if ev["kind"] == "checkpoint" else "")
        print(f"  {stamp}  {ev['kind']:10} {who:28} {detail}")


def main(argv=None):
    args = parser().parse_args(argv)
    if getattr(args, "from_hook", False):
        interactive.run_hook(emit=lambda line: print(f"relay: {line}", file=sys.stderr))
        return
    try:
        if args.action == "demo":
            from .demo import run
            run()
            return
        if args.action == "doctor":
            print(f"Agent Relay {__version__} | Python {sys.version.split()[0]} | {sys.platform}")
            for program in ("git", "gh", "codex", "claude"):
                print(f"{program}: {shutil.which(program) or 'not installed'}")
            print("macOS/Linux supported. Authentication and real provider handoffs are not tested by doctor.")
            return
        repo = repository()
        if args.action == "init":
            Store(repo, args.remote)
            output(repo, "var", "GIT_AUTHOR_IDENT")
            check = shlex.split(args.check) if args.check else None
            slug = None if args.no_github else detect(repo, args.remote)
            fields = {"github": slug,
                      "owner": output(repo, "config", "user.name") or None,
                      "default_branch": default_branch(repo, args.remote)}
            if slug:
                from .github import GitHub
                gh = GitHub(slug)
                fields["owner"] = gh.login()
                gh.ensure_label()
            cfg = config.initialize(repo, args.remote, check, **fields)
            print(f"Enrolled this clone as {cfg['owner']} (worker {cfg['worker']}, {cfg['mode']}).")
            print(f"GitHub conversation: {slug or 'off'}. Code and task text are shared; logs and credentials stay local.")
            print("Next: relay add 'your task' (or label an issue 'relay'), then relay start --agent claude")
            return
        cfg = config.load(repo)
        store = Store(repo, cfg["remote"])
        who = f"{cfg.get('owner') or cfg['worker']}"
        talk = Conversation(repo, {**cfg, "label": who}, store)
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
            task_id = tasks.add(store, args.prompt, sha, base_branch=base, created_by=cfg["worker"],
                                after=args.after, by=who, requested_by=cfg.get("owner"), for_=args.for_)
            task = talk.flush(store.read()[1]["tasks"][task_id])
            issue = f" as issue #{task['issue']}" if task.get("issue") else ""
            print(f"Queued {task_id}{issue} from {base}@{sha[:10]}. Task text is visible to repository collaborators.")
        elif args.action == "start":
            if os.name != "posix":
                raise RelayError("Workers currently require macOS or Linux")
            if not shutil.which(args.agent):
                raise RelayError(f"Install and sign in to {args.agent} first")
            if min(args.checkpoint_seconds, args.cooldown_seconds, args.max_runtime_seconds) < 10:
                raise RelayError("Timing options must be at least 10 seconds")
            if args.accept_from is not None or args.max_tasks_per_day is not None:
                config.update(repo, accept_from=[x.strip() for x in (args.accept_from or "").split(",") if x.strip()] or None,
                              max_tasks_per_day=args.max_tasks_per_day)
            worker = Worker(repo, args.agent, model=args.model, interval=args.checkpoint_seconds,
                            cooldown=args.cooldown_seconds, max_runtime=args.max_runtime_seconds)
            def stop(signum, frame):
                worker.stopping = True
            previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGINT, signal.SIGTERM)}
            print(f"{worker.label} is available for this repository. Ctrl-C checkpoints and hands off.")
            try:
                worker.run(once=args.once, opt_in=True)
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        elif args.action == "handoff":
            interactive.handoff(repo, cfg, store, message=args.message, review=args.review)
        elif args.action == "take":
            interactive.take(repo, cfg, store, args.task)
        elif args.action == "hook":
            path = interactive.install_hook(repo, uninstall=args.verb == "uninstall")
            if args.verb == "install":
                config.update(repo, share_prompt=args.share_prompt)
                print(f"Installed a StopFailure(rate_limit) hook in {path} (kept out of Git).")
                print("When Claude Code hits a usage limit in this clone, your work is published for the next agent.")
                if not args.share_prompt:
                    print("Your prompts stay private; the next agent sees your code, commits and .relay-handoff.md.")
            else:
                print(f"Removed the Agent Relay hook from {path}.")
        elif args.action == "status":
            _, state = store.read()
            if args.json:
                print(json.dumps({"worker": cfg, "state": state}, indent=2))
                return
            if cfg.get("cooldown_until", 0) > time.time():
                print(f"This clone is cooling down for {int(cfg['cooldown_until'] - time.time())}s after a usage limit.\n")
            board(cfg, state)
        elif args.action == "show":
            _, state = store.read()
            if args.task not in state["tasks"]:
                raise RelayError("Unknown task")
            task = state["tasks"][args.task]
            if args.json:
                print(json.dumps(task, indent=2))
                return
            print(f"{task['id']}  {task['status']}  attempt {task['epoch']}")
            print(task["prompt"])
            if task.get("issue") and cfg.get("github"):
                print(f"Issue: https://github.com/{cfg['github']}/issues/{task['issue']}")
            print("\nTIMELINE")
            timeline(task)
            for item in task.get("notes", []):
                print(f"  note from {item.get('by')}: {item['text']}")
            cp = task.get("checkpoint")
            if cp:
                print(f"\nLatest checkpoint {cp['sha'][:10]} on {cp['branch']}: {(cp.get('summary') or {}).get('stat', '')}")
                print(f"Review without changing your branch:\n  git fetch {shlex.quote(cfg['remote'])} {shlex.quote(cp['branch'])}\n  git diff {task['base'][:10]} {cp['sha'][:10]}")
                print(f"Or continue it yourself: relay take {task['id']}")
        elif args.action == "note":
            tasks.add_note(store, args.task, args.text, worker=cfg["worker"], by=who)
            talk.flush(store.read()[1]["tasks"][args.task])
            print(f"Noted. The next agent on {args.task} will read it.")
        elif args.action == "retry":
            tasks.retry(store, args.task, note=args.note, worker=cfg["worker"], by=who)
            talk.flush(store.read()[1]["tasks"][args.task])
            print(f"Queued {args.task}")
        elif args.action == "cancel":
            tasks.cancel(store, args.task, worker=cfg["worker"], by=who)
            talk.flush(store.read()[1]["tasks"][args.task])
            print(f"Cancelled {args.task}")
        elif args.action == "sync":
            if not talk.gh:
                raise RelayError("GitHub conversation is off for this clone (re-run relay init with gh signed in)")
            report = talk.sync(cfg.get("default_branch") or "main")
            print(f"Imported issues: {report['imported'] or 'none'} | commands: {len(report['commands'])} | merged: {report['done'] or 'none'}")
    except (RelayError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"relay: {exc}", file=sys.stderr)
        sys.exit(1)


def default_branch(repo, remote):
    result = git(repo, "ls-remote", "--symref", remote, "HEAD", check=False)
    for line in result.stdout.decode().splitlines():
        if line.startswith("ref: refs/heads/"):
            return line.split()[1].removeprefix("refs/heads/")
    return "main"

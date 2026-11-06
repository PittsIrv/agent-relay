"""Hand off and take back work from a person's own interactive coding session."""

import contextlib
import fcntl
import json
import shlex
import sys
import time
from pathlib import Path

from . import config, tasks
from .checkpoint import publish, snapshot, summarize
from .conversation import Conversation
from .gitstore import RelayError, git, output, private_dir

HOOK_TAG = "agent_relay handoff --from-hook"


def current_branch(repo):
    result = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return result.stdout.decode().strip() or None


def remote_head(repo, remote, branch):
    refs = output(repo, "ls-remote", "--refs", remote, f"refs/heads/{branch}") if branch else ""
    return refs.split()[0] if refs else None


def label(cfg, agent="session"):
    return f"{cfg.get('owner') or cfg['worker']}'s {agent}"


def last_prompt(transcript):
    """Most recent typed user prompt from a Claude Code transcript (opt-in only)."""
    try:
        lines = Path(transcript).expanduser().read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        message = entry.get("message") if isinstance(entry, dict) else None
        if entry.get("type") == "user" and isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"].strip()[:4000]
    return None


def compose_prompt(repo, owner, branch, base, tree, message, shared_prompt):
    files = output(repo, "diff", "--name-only", base, tree).splitlines() if base else []
    where = f"`{branch}`" if branch else "a detached HEAD"
    touched = f" ({', '.join(files[:3])}{', ...' if len(files) > 3 else ''})" if files else ""
    if message:
        parts = [message]
    elif shared_prompt:
        parts = [shared_prompt.splitlines()[0][:200],
                 f"{owner}'s interactive session was cut off while working on this. Their full request was:\n> "
                 + shared_prompt.replace("\n", "\n> ")]
    else:
        parts = [f"Continue {owner}'s in-progress work on {where}{touched}"]
    log = output(repo, "log", "--format=- %s", "-8", f"{base}..HEAD") if base else ""
    if log:
        parts.append(f"Recent commits on that branch:\n{log}")
    parts.append("Read .relay-handoff.md if it exists, inspect the diff against the base, run the tests, then finish the work.")
    return "\n\n".join(parts)


@contextlib.contextmanager
def one_at_a_time(repo):
    """Take, hand-off and the hook never interleave within one clone."""
    folder = private_dir(repo)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "interactive.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _local_state(repo):
    """The clone's hold bookkeeping, read fresh."""
    fresh = config.load(repo)
    return {k: fresh[k] for k in ("held", "handed_off") if k in fresh}


def handoff(repo, cfg, store, **kwargs):
    """Snapshot this clone's working tree (index and HEAD untouched) and publish it as a task."""
    with one_at_a_time(repo):
        # Reload inside the lock: a take that finished while this waited changed it.
        return _handoff(repo, {**cfg, **_local_state(repo)}, store, **kwargs)


def _handoff(repo, cfg, store, *, message=None, review=False, reason=None, agent="session",
             hook=None, emit=print):
    remote = cfg["remote"]
    branch = current_branch(repo)
    who = label(cfg, agent)
    talk = Conversation(repo, {**cfg, "label": who}, store, emit)
    held = (cfg.get("held") or {}).get(branch or "")
    head = output(repo, "rev-parse", "HEAD")
    tree = output(repo, "rev-parse", f"{snapshot(repo, head, 'probe', 0)}^{{tree}}")
    _, state = store.read()
    task = state["tasks"].get(held) if held else None
    if task and not (task["status"] == "held" and task["lease"] and task["lease"]["worker"] == cfg["worker"]):
        if task["status"] == "done":
            emit(f"{task['id']} was completed while you held it (its PR merged); publishing your work as a new task.")
        task = None  # Someone released or took it in the meantime; publish fresh work instead.
    if task is None and branch and hook:
        previous = (cfg.get("handed_off") or {}).get(branch)
        known = previous and state["tasks"].get(previous["task"])
        if known and tree == previous.get("tree"):
            emit(f"Already handed off as {known['id']} ({known['status']}); nothing new to publish.")
            return known
        if known and known["status"] == "queued":
            task = tasks.claim(store, cfg["worker"], by=who, task_id=known["id"])
            if task is None:
                emit(f"{known['id']} was just claimed by someone else; not publishing a second copy.")
                return known
        # Otherwise the earlier task moved on (running, review, done) and this is new work.

    git(repo, "fetch", "--quiet", "--no-tags", remote)
    if task:
        base, base_branch = task["base"], task.get("base_branch")
    else:
        base_branch = branch if remote_head(repo, remote, branch) else cfg.get("default_branch") or "main"
        remote_base = remote_head(repo, remote, base_branch)
        if not remote_base:
            raise RelayError(f"Base branch {base_branch!r} is not on {remote}; push it first")
        git(repo, "fetch", "--quiet", "--no-tags", remote, remote_base)
        merged = git(repo, "merge-base", "HEAD", remote_base, check=False)
        base = merged.stdout.decode().strip() or remote_base
        if tree == output(repo, "rev-parse", f"{base}^{{tree}}") and not message:
            emit("No changes since the base branch; nothing to hand off.")
            return None
        shared = last_prompt(hook["transcript_path"]) if hook and cfg.get("share_prompt") and hook.get("transcript_path") else None
        task = tasks.create_held(store, compose_prompt(repo, cfg.get("owner") or "a collaborator", branch, base, tree, message, shared), base, base_branch,
                                 cfg["worker"], by=who, requested_by=cfg.get("owner"))

    epoch = task["epoch"]
    sha = snapshot(repo, head, task["id"], epoch, label="interactive handoff")
    tree = output(repo, "rev-parse", f"{sha}^{{tree}}")  # What was actually published.
    cp = publish(repo, remote, sha, task["id"], epoch)
    cp["summary"] = summarize(repo, base, sha)
    status = "review" if review else "queued"
    why = reason or ("finished interactively; ready for review" if review else "handed off by the owner")
    if message and task.get("notes") is not None and held:
        tasks.add_note(store, task["id"], message, worker=cfg["worker"], by=who)
    task = tasks.update(store, task["id"], cfg["worker"], epoch, checkpoint=cp, status=status,
                        kind="review" if review else "handoff", reason=why, by=who)
    config.update(repo, held={k: v for k, v in (cfg.get("held") or {}).items() if k != branch},
                  handed_off={**(cfg.get("handed_off") or {}), **({branch: {"task": task["id"], "sha": sha, "tree": tree}} if branch else {})})
    task = talk.flush(task)
    emit(f"Published {task['id']} at {sha[:10]} on {cp['branch']} ({cp['summary']['stat'] or 'no file changes'}).")
    emit("Your branch, index and files are unchanged. " + (
        "Marked for review." if review else "The next available agent can continue it."))
    return task


def take(repo, cfg, store, task_id, emit=print):
    with one_at_a_time(repo):
        return _take(repo, {**cfg, **_local_state(repo)}, store, task_id, emit)


def _take(repo, cfg, store, task_id, emit=print):
    """Hold a task for interactive work and check out its latest checkpoint on a local branch."""
    if output(repo, "status", "--porcelain"):
        raise RelayError("Commit or stash your local changes before taking a task")
    branch = f"relay/{task_id}"
    if git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).returncode == 0:
        raise RelayError(f"Local branch {branch} already exists; rename or delete it first")
    who = label(cfg)
    current = store.read()[1]["tasks"].get(task_id)
    if not current:
        raise RelayError("Unknown task")
    # Fetch before holding so a network failure cannot leave a hold nobody can find.
    git(repo, "fetch", "--quiet", "--no-tags", cfg["remote"], (current.get("checkpoint") or {}).get("sha") or current["base"])
    task = tasks.hold(store, task_id, cfg["worker"], by=who)
    start = (task.get("checkpoint") or {}).get("sha") or task["base"]
    try:
        git(repo, "fetch", "--quiet", "--no-tags", cfg["remote"], start)
        git(repo, "switch", "--quiet", "-c", branch, start)
        config.update(repo, held={**(cfg.get("held") or {}), branch: task_id})
    except Exception:
        tasks.unhold(store, task_id, cfg["worker"], task["epoch"], by=who)
        raise
    Conversation(repo, {**cfg, "label": who}, store, emit).flush(task)
    emit(f"Holding {task_id} on local branch {branch} at {start[:10]}. Agents will not touch it.")
    emit("Work as usual. `relay handoff` gives it back to the pool; `relay handoff --review` marks it finished.")
    return task


def run_hook(repo_hint=None, stdin=None, emit=print):
    """Entry point for Claude Code's StopFailure hook. Always exits quietly."""
    raw = (stdin or sys.stdin).read()
    try:
        payload = json.loads(raw or "{}")
    except ValueError:
        payload = {}
    if payload.get("error") != "rate_limit":
        return None
    from .gitstore import Store, repository
    try:
        repo = repository(payload.get("cwd") or repo_hint or ".")
    except RelayError:
        return None  # Not inside a Git repository: nothing to hand off.
    log = private_dir(repo) / "hook.log"
    lines = []
    try:
        cfg = config.load(repo)
        store = Store(repo, cfg["remote"])
        task = handoff(repo, cfg, store, hook=payload, agent="Claude Code session",
                       reason="usage limit reached", emit=lines.append)
        return task
    except Exception as exc:  # A hook must never disturb the session; record why locally.
        lines.append(f"handoff failed: {exc}")
        return None
    finally:
        try:
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a") as handle:
                for line in lines:
                    handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {line}\n")
        except OSError:
            pass
        for line in lines:
            emit(line)


def hook_command():
    return f"{shlex.quote(sys.executable)} -m {HOOK_TAG}"


def install_hook(repo, uninstall=False):
    """Add or remove a project-local Claude Code StopFailure(rate_limit) hook."""
    folder = Path(repo) / ".claude"
    path = folder / "settings.local.json"
    try:
        settings = json.loads(path.read_text()) if path.exists() else {}
    except ValueError as exc:
        raise RelayError(f"{path} is not valid JSON; fix it before installing the hook") from exc
    groups = settings.setdefault("hooks", {}).setdefault("StopFailure", [])
    kept = [g for g in groups if not any(HOOK_TAG in h.get("command", "") for h in g.get("hooks", []))]
    if not uninstall:
        kept.append({"matcher": "rate_limit", "hooks": [{"type": "command", "command": hook_command()}]})
    settings["hooks"]["StopFailure"] = kept
    if not kept:
        del settings["hooks"]["StopFailure"]
    folder.mkdir(exist_ok=True)
    path.write_text(json.dumps(settings, indent=2) + "\n")
    exclude = Path(output(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")) / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if ".claude/settings.local.json" not in lines:
        exclude.write_text("\n".join([*lines, ".claude/settings.local.json"]) + "\n")
    return path

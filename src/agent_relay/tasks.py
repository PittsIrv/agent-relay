"""Task state transitions. Every mutate() may rerun on contention: no side effects."""

import copy
import time
import uuid

from .gitstore import RelayError

HELD_FOREVER = 10 * 365 * 86400
REQUEUEABLE = ("blocked", "review", "cancelled")


class LeaseLost(RelayError):
    pass


def event(task, kind, **fields):
    task["seq"] = task.get("seq", 0) + 1
    task["events"].append({"n": task["seq"], "at": time.time(), "kind": kind, **fields})
    task["events"] = task["events"][-50:]


def new_task(prompt, base, *, base_branch=None, created_by=None, after=(), issue=None):
    return {
        "id": uuid.uuid4().hex[:10], "prompt": prompt, "base": base,
        "base_branch": base_branch, "status": "queued", "epoch": 0,
        "checkpoint": None, "created_at": time.time(), "created_by": created_by,
        "after": list(after), "issue": issue, "pr": None, "notes": [],
        "events": [], "seq": 0, "lease": None,
    }


def add(store, prompt, base, *, base_branch=None, created_by=None, after=(), by=None):
    task = new_task(prompt, base, base_branch=base_branch, created_by=created_by, after=after)

    def mutate(state):
        for dep in task["after"]:
            if dep not in state["tasks"]:
                raise RelayError(f"Unknown dependency {dep}")
        fresh = copy.deepcopy(task)
        event(fresh, "created", worker=created_by, by=by)
        state["tasks"][fresh["id"]] = fresh
        return fresh["id"]

    return store.change(mutate)


def import_issue(store, number, prompt, base, base_branch, worker, by):
    """Idempotent: an issue already linked to a task is never imported twice."""
    task = new_task(prompt, base, base_branch=base_branch, created_by=worker, issue=number)

    def mutate(state):
        if any(t.get("issue") == number for t in state["tasks"].values()):
            return None
        fresh = copy.deepcopy(task)
        event(fresh, "imported", worker=worker, by=by)
        state["tasks"][fresh["id"]] = fresh
        return fresh["id"]

    return store.change(mutate)


def ready(task, state):
    deps = (state["tasks"].get(dep) for dep in task.get("after", []))
    return all(dep is not None and dep["status"] == "done" for dep in deps)


def claim(store, worker, ttl=300, now=None, by=None, task_id=None):
    def mutate(state):
        current = time.time() if now is None else now
        for task in sorted(state["tasks"].values(), key=lambda t: t["created_at"]):
            if task_id and task["id"] != task_id:
                continue
            expired = task["status"] == "running" and task["lease"]["until"] < current
            if not expired and (task["status"] != "queued" or not ready(task, state)):
                continue
            task["epoch"] += 1
            task["status"] = "running"
            task["lease"] = {"worker": worker, "epoch": task["epoch"], "until": current + ttl}
            event(task, "claimed", worker=worker, by=by, epoch=task["epoch"], recovered=expired)
            return copy.deepcopy(task)
        return None
    return store.change(mutate)


def hold(store, task_id, worker, by=None, now=None):
    """A human takes a task to work on interactively; workers skip it until handoff."""
    def mutate(state):
        current = time.time() if now is None else now
        task = state["tasks"].get(task_id)
        if not task:
            raise RelayError("Unknown task")
        expired = task["status"] == "running" and task["lease"]["until"] < current
        if task["status"] not in ("queued", *REQUEUEABLE) and not expired:
            raise RelayError(f"Task is {task['status']}; only queued, review, blocked or cancelled tasks can be taken")
        task["epoch"] += 1
        task["status"] = "held"
        task["lease"] = {"worker": worker, "epoch": task["epoch"], "until": current + HELD_FOREVER, "human": True}
        event(task, "held", worker=worker, by=by, epoch=task["epoch"])
        return copy.deepcopy(task)
    return store.change(mutate)


def create_held(store, prompt, base, base_branch, worker, by=None):
    """Publish an interactive session's in-progress work as a new task held by its owner."""
    task = new_task(prompt, base, base_branch=base_branch, created_by=worker)

    def mutate(state):
        fresh = copy.deepcopy(task)
        fresh.update(epoch=1, status="held")
        fresh["lease"] = {"worker": worker, "epoch": 1, "until": time.time() + HELD_FOREVER, "human": True}
        event(fresh, "created", worker=worker, by=by, interactive=True)
        state["tasks"][fresh["id"]] = fresh
        return copy.deepcopy(fresh)

    return store.change(mutate)


def update(store, task_id, worker, epoch, *, ttl=300, checkpoint=None, status=None,
           reason=None, verification=None, base=None, kind=None, by=None, now=None, **extra):
    def mutate(state):
        current = time.time() if now is None else now
        task = state["tasks"].get(task_id)
        lease = task and task.get("lease")
        if not lease or lease["worker"] != worker or lease["epoch"] != epoch or lease["until"] < current:
            raise LeaseLost("Task lease lost; this attempt may no longer publish task state")
        if not lease.get("human"):
            lease["until"] = current + ttl
        if base is not None:
            task["base"] = base
        if checkpoint is not None:
            task["checkpoint"] = checkpoint
            event(task, "checkpoint", worker=worker, sha=checkpoint["sha"], epoch=epoch)
        if verification is not None:
            task["verification"] = verification
        if status:
            task["status"] = status
            task["lease"] = None
            event(task, kind or status, reason=reason or "", worker=worker, by=by, epoch=epoch, **extra)
        return copy.deepcopy(task)
    return store.change(mutate)


def annotate(store, task_id, **fields):
    """Record GitHub issue/PR numbers once; never overwrites an existing link."""
    def mutate(state):
        task = state["tasks"].get(task_id)
        if not task:
            raise RelayError("Unknown task")
        missing = {k: v for k, v in fields.items() if task.get(k) is None}
        if not missing:
            return None
        task.update(missing)
        return copy.deepcopy(task)
    return store.change(mutate) or store.read()[1]["tasks"][task_id]


def _note(task, text, by):
    if text:
        task.setdefault("notes", []).append({"at": time.time(), "by": by, "text": text[:2000]})
        task["notes"] = task["notes"][-20:]


def retry(store, task_id, note=None, worker=None, by=None):
    def mutate(state):
        task = state["tasks"].get(task_id)
        if not task:
            raise RelayError("Unknown task")
        if task["status"] not in REQUEUEABLE:
            raise RelayError("Only blocked, review or cancelled tasks can be retried")
        task["status"] = "queued"
        _note(task, note, by)
        event(task, "retried", worker=worker, by=by, note=note or "")
        return task_id
    return store.change(mutate)


def add_note(store, task_id, text, worker=None, by=None):
    def mutate(state):
        task = state["tasks"].get(task_id)
        if not task:
            raise RelayError("Unknown task")
        _note(task, text, by)
        event(task, "note", worker=worker, by=by, note=text[:2000])
        return task_id
    return store.change(mutate)


def cancel(store, task_id, worker=None, by=None):
    def mutate(state):
        task = state["tasks"].get(task_id)
        if not task:
            raise RelayError("Unknown task")
        if task["status"] in ("done", "cancelled"):
            return None
        task["status"] = "cancelled"
        task["lease"] = None  # A running attempt loses its lease and stops at its next renewal.
        event(task, "cancelled", worker=worker, by=by)
        return task_id
    return store.change(mutate)


def mark_done(store, task_id, pr, worker=None):
    def mutate(state):
        task = state["tasks"].get(task_id)
        if not task or task["status"] != "review":
            return None
        task["status"] = "done"
        event(task, "done", worker=worker, pr=pr)
        return task_id
    return store.change(mutate)


def command(store, comment_id, task_id, verb, text, worker, by):
    """Apply one GitHub comment command at most once, recording its id under CAS."""
    def mutate(state):
        handled = state.setdefault("handled", [])
        if comment_id in handled:
            return None
        handled.append(comment_id)
        state["handled"] = handled[-500:]
        task = state["tasks"].get(task_id)
        if not task:
            return "ignored"
        if verb == "note" and text:
            _note(task, text, by)
            event(task, "note", worker=worker, by=by, via="github")
            return "noted"
        if verb == "retry" and task["status"] in REQUEUEABLE:
            task["status"] = "queued"
            _note(task, text, by)
            event(task, "retried", worker=worker, by=by, note=text, via="github")
            return "requeued"
        if verb == "cancel" and task["status"] not in ("done", "cancelled"):
            task["status"] = "cancelled"
            task["lease"] = None
            event(task, "cancelled", worker=worker, by=by, via="github")
            return "cancelled"
        return "ignored"
    return store.change(mutate)


def presence(store, worker, info, now=None):
    def mutate(state):
        state.setdefault("workers", {})[worker] = {**info, "last_seen": time.time() if now is None else now}
        return True
    return store.change(mutate)

"""Task state transitions. Every mutate() may rerun on contention: no side effects."""

import copy
import time
import uuid

from .gitstore import RelayError

HELD_FOREVER = 10 * 365 * 86400
REQUEUEABLE = ("blocked", "review", "cancelled")
HANDLED_KEEP = 5000


class LeaseLost(RelayError):
    pass


def event(task, kind, **fields):
    """Keep the last 50 narrated events and the last 10 checkpoints separately, so frequent
    checkpoints during a GitHub outage cannot push unposted narration out of state."""
    task["seq"] = task.get("seq", 0) + 1
    task["events"].append({"n": task["seq"], "at": time.time(), "kind": kind, **fields})
    narrated = [ev for ev in task["events"] if ev["kind"] != "checkpoint"][-50:]
    checkpoints = [ev for ev in task["events"] if ev["kind"] == "checkpoint"][-10:]
    task["events"] = sorted(narrated + checkpoints, key=lambda ev: ev.get("n", 0))


def new_task(prompt, base, *, base_branch=None, created_by=None, after=(), issue=None,
             requested_by=None, for_=()):
    return {
        "id": uuid.uuid4().hex[:10], "prompt": prompt, "base": base,
        "base_branch": base_branch, "status": "queued", "epoch": 0,
        "checkpoint": None, "created_at": time.time(), "created_by": created_by,
        "after": list(after), "issue": issue, "pr": None, "notes": [],
        "events": [], "seq": 0, "lease": None,
        "requested_by": requested_by, "for": sorted(set(for_)),
    }


def add(store, prompt, base, *, base_branch=None, created_by=None, after=(), by=None,
        requested_by=None, for_=()):
    task = new_task(prompt, base, base_branch=base_branch, created_by=created_by, after=after,
                    requested_by=requested_by, for_=for_)

    def mutate(state):
        for dep in task["after"]:
            if dep not in state["tasks"]:
                raise RelayError(f"Unknown dependency {dep}")
        fresh = copy.deepcopy(task)
        event(fresh, "created", worker=created_by, by=by)
        state["tasks"][fresh["id"]] = fresh
        return fresh["id"]

    return store.change(mutate)


def import_issue(store, number, prompt, base, base_branch, worker, by, requested_by=None, for_=()):
    """Idempotent: an issue already linked to a task is never imported twice."""
    task = new_task(prompt, base, base_branch=base_branch, created_by=worker, issue=number,
                    requested_by=requested_by, for_=for_)

    def mutate(state):
        if any(t.get("issue") == number for t in state["tasks"].values()):
            return None
        fresh = copy.deepcopy(task)
        event(fresh, "imported", worker=worker, by=by)
        state["tasks"][fresh["id"]] = fresh
        return fresh["id"]

    return store.change(mutate)


def retire_pr(task):
    """A new attempt gets its own PR; the old one can no longer complete the task.
    Returns the retired PR number so the acting worker can close it on GitHub."""
    old = task.get("pr")
    if old:
        task.setdefault("past_prs", []).append({"pr": old, "epoch": task.get("pr_epoch")})
    task["pr"] = None
    for key in ("verification", "pr_epoch", "pr_draft", "result_epoch"):
        task.pop(key, None)
    return old


def current_result(task):
    """The attempt whose review result (and PR) is current. A reversible take keeps it, so the
    epoch itself stays a monotonic fencing token and is never restored."""
    held = task["status"] == "held" and (task.get("held_from") or {}).get("status") == "review"
    if task["status"] == "review" or held:
        return task.get("result_epoch", (task.get("held_from") or {}).get("epoch", task["epoch"]))
    return None


def facts(task):
    """Event-time snapshot so delayed narration describes this moment, not a later one."""
    cp = task.get("checkpoint")
    return {"checkpoint": {k: cp[k] for k in ("sha", "branch", "summary") if k in cp}} if cp else {}


def ready(task, state):
    deps = (state["tasks"].get(dep) for dep in task.get("after", []))
    return all(dep is not None and dep["status"] == "done" for dep in deps)


def allowed(task, policy):
    """Owner consent: whose requests this worker accepts, and tasks addressed to someone else."""
    if not policy:
        return True
    owner = policy.get("owner")
    wanted = task.get("for") or []
    if wanted and owner not in wanted:
        return False
    accept = policy.get("accept_from")
    requester = task.get("requested_by")
    return not accept or requester == owner or requester in accept


def claim(store, worker, ttl=300, now=None, by=None, task_id=None, policy=None):
    def mutate(state):
        current = time.time() if now is None else now
        for task in sorted(state["tasks"].values(), key=lambda t: t["created_at"]):
            if (task_id and task["id"] != task_id) or not allowed(task, policy):
                continue
            expired = task["status"] == "running" and task["lease"]["until"] < current
            if not expired and (task["status"] != "queued" or not ready(task, state)):
                continue
            task["epoch"] += 1
            task["status"] = "running"
            task["lease"] = {"worker": worker, "epoch": task["epoch"], "until": current + ttl}
            event(task, "claimed", worker=worker, by=by, epoch=task["epoch"], recovered=expired,
                  start=(task.get("checkpoint") or {}).get("sha"), base=task["base"])
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
        # A hold is reversible until the person hands work back, so its PR stays linked (and
        # protected from retirement) until then; only the status is restored if taking fails.
        task["held_from"] = {"status": task["status"], "epoch": task["epoch"]}
        if task["status"] == "review":
            task.setdefault("result_epoch", task["epoch"])
        task["epoch"] += 1
        task["status"] = "held"
        task["lease"] = {"worker": worker, "epoch": task["epoch"], "until": current + HELD_FOREVER, "human": True}
        event(task, "held", worker=worker, by=by, epoch=task["epoch"])
        return copy.deepcopy(task)
    return store.change(mutate)


def unhold(store, task_id, worker, epoch, by=None):
    """Undo a hold whose local checkout failed: restore the prior status."""
    def mutate(state):
        task = state["tasks"].get(task_id)
        lease = task and task.get("lease")
        if not lease or lease["worker"] != worker or lease["epoch"] != epoch or task["status"] != "held":
            raise LeaseLost("Task is no longer held by this clone")
        previous = (task.pop("held_from", None) or {}).get("status", "queued")
        # An expired running task was reclaimable anyway. The epoch is not restored; the held
        # attempt's result stays current through result_epoch.
        task["status"] = "queued" if previous == "running" else previous
        task["lease"] = None
        event(task, "released", worker=worker, by=by, epoch=epoch, restored=task["status"])
        return copy.deepcopy(task)
    return store.change(mutate)


def attach_pr(store, task_id, epoch, number, draft, worker=None):
    """Link a PR only to the attempt that produced it. A stale PR is refused and recorded
    as superseded first, so closing it on GitHub survives a crash or an outage."""
    def mutate(state):
        task = state["tasks"].get(task_id)
        if not task:
            return None
        if task.get("pr") == number:
            return "attached"  # Already the current PR (a repeat).
        if any(p.get("pr") == number for p in task.get("past_prs", [])):
            return "superseded"  # Retirement is terminal.
        # Only the attempt whose result is current may publish, also during a reversible take.
        if current_result(task) != epoch or task.get("pr") is not None:
            if not any(p.get("pr") == number for p in task.get("past_prs", [])):
                task.setdefault("past_prs", []).append({"pr": number, "epoch": epoch})
                event(task, "superseded", worker=worker, epoch=epoch, retired_pr=number)
            return "superseded"
        task.update(pr=number, pr_epoch=epoch, pr_draft=draft)
        return "attached"
    return store.change(mutate) == "attached"


def create_held(store, prompt, base, base_branch, worker, by=None, requested_by=None):
    """Publish an interactive session's in-progress work as a new task held by its owner."""
    task = new_task(prompt, base, base_branch=base_branch, created_by=worker, requested_by=requested_by)

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
            if verification is None:
                task.pop("verification", None)  # A check result belongs to the checkpoint it ran on.
            event(task, "checkpoint", worker=worker, sha=checkpoint["sha"], epoch=epoch)
        # New work from a later attempt supersedes an earlier attempt's PR, irreversibly; retire
        # it before recording this attempt's own check result.
        retired = retire_pr(task) if status and task.get("pr") and task.get("pr_epoch") != epoch else None
        if verification is not None:
            task["verification"] = verification
        if status:
            task["status"] = status
            task["lease"] = None
            task.pop("held_from", None)
            if status == "review":
                task["result_epoch"] = epoch
            else:
                task.pop("result_epoch", None)
            check = (task.get("verification") or {}) if status == "review" else {}
            event(task, kind or status, reason=reason or "", worker=worker, by=by, epoch=epoch,
                  check=check.get("result"), changed_during_check=check.get("snapshot_changed_during_check"),
                  retired_pr=retired, **facts(task), **extra)
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
        old = retire_pr(task)
        _note(task, note, by)
        event(task, "retried", worker=worker, by=by, note=note or "", retired_pr=old)
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
        if not task or task.get("pr") != pr:
            return None
        # A PR merged while its task was taken still completes it; the holder's branch stays local.
        if task.get("pr_epoch") is None or task.get("pr_epoch") != current_result(task):
            return None
        task["status"] = "done"
        task["lease"] = None
        task.pop("held_from", None)
        event(task, "done", worker=worker, pr=pr)
        return task_id
    return store.change(mutate)


def command(store, comment_id, task_id, verb, text, worker, by):
    """Apply a GitHub comment command once while its id is among the last HANDLED_KEEP ids."""
    def mutate(state):
        handled = state.setdefault("handled", [])
        if comment_id in handled:
            return None
        handled.append(comment_id)
        state["handled"] = handled[-HANDLED_KEEP:]
        task = state["tasks"].get(task_id)
        if not task:
            return "ignored"
        if verb == "note" and text:
            _note(task, text, by)
            event(task, "note", worker=worker, by=by, via="github")
            return "noted"
        if verb == "retry" and task["status"] in REQUEUEABLE:
            task["status"] = "queued"
            old = retire_pr(task)
            _note(task, text, by)
            event(task, "retried", worker=worker, by=by, note=text, via="github", retired_pr=old)
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

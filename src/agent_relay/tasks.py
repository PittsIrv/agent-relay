import copy
import time
import uuid

from .gitstore import RelayError


def add(store, prompt, base):
    task_id = uuid.uuid4().hex[:10]

    def mutate(state):
        state["tasks"][task_id] = {
            "id": task_id, "prompt": prompt, "base": base, "status": "queued",
            "epoch": 0, "checkpoint": None, "created_at": time.time(),
            "events": [], "lease": None,
        }
        return task_id

    return store.change(mutate)


def event(task, kind, **fields):
    task["events"].append({"at": time.time(), "kind": kind, **fields})
    task["events"] = task["events"][-50:]


def claim(store, worker, ttl=300, now=None):
    def mutate(state):
        current = time.time() if now is None else now
        for task in sorted(state["tasks"].values(), key=lambda t: t["created_at"]):
            expired = task["status"] == "running" and task["lease"]["until"] < current
            if task["status"] != "queued" and not expired:
                continue
            task["epoch"] += 1
            task["status"] = "running"
            task["lease"] = {"worker": worker, "epoch": task["epoch"], "until": current + ttl}
            event(task, "claimed", worker=worker, epoch=task["epoch"], recovered=expired)
            return copy.deepcopy(task)
        return None
    return store.change(mutate)


def update(store, task_id, worker, epoch, *, ttl=300, checkpoint=None, status=None,
           reason=None, verification=None, now=None):
    def mutate(state):
        current = time.time() if now is None else now
        task = state["tasks"].get(task_id)
        lease = task and task.get("lease")
        if not lease or lease["worker"] != worker or lease["epoch"] != epoch or lease["until"] < current:
            raise RelayError("Task lease lost; this attempt may no longer publish task state")
        lease["until"] = current + ttl
        if checkpoint is not None:
            task["checkpoint"] = checkpoint
            event(task, "checkpoint", sha=checkpoint["sha"], epoch=epoch)
        if verification is not None:
            task["verification"] = verification
        if status:
            task["status"] = status
            task["lease"] = None
            event(task, status, reason=reason or "", worker=worker)
        return copy.deepcopy(task)
    return store.change(mutate)


def retry(store, task_id):
    def mutate(state):
        task = state["tasks"].get(task_id)
        if not task:
            raise RelayError("Unknown task")
        if task["status"] not in ("blocked", "review"):
            raise RelayError("Only blocked or review tasks can be retried")
        task["status"] = "queued"
        event(task, "retried")
        return task_id
    return store.change(mutate)

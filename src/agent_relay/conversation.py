"""Narrate task events on GitHub issues and PRs, and read collaborator commands back.

Each event is posted by the worker that caused it, after checking the thread for its
marker. Shared state on the relay/state branch stays authoritative; a GitHub outage
delays comments, and only an event that ages out of state before its worker
returns is never narrated.
"""

import fcntl
import json
import time
from datetime import datetime, timezone

from . import tasks
from .gitstore import RelayError, output, private_dir
from .github import GitHub, Truncated

MARKER = "<!-- relay:{task}:{n} -->"
OWNED = "<!-- relay-task:{task} -->"
RETIRED = "<!-- relay-retired:{task}:{pr} -->"
SILENT = {"checkpoint", "created"}
DEFERRABLE = (RelayError, OSError, ValueError, KeyError, TypeError)  # Retried on a later flush.
VERBS = ("note", "retry", "cancel")


def title(task):
    first = " ".join(task["prompt"].split("\n", 1)[0].split()) or "Relay task"
    return first if len(first) <= 80 else first[:77] + "..."


def short(sha):
    return (sha or "")[:7]


class Conversation:
    def __init__(self, repo, cfg, store, emit=print):
        self.repo = repo
        self.cfg = cfg
        self.store = store
        self.worker = cfg["worker"]
        self.emit = emit
        self.gh = GitHub(cfg["github"], cfg.get("gh")) if cfg.get("github") else None
        self.folder = private_dir(repo)

    # Local bookkeeping -------------------------------------------------

    def _load(self, name, default):
        path = self.folder / name
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return default

    def _save(self, name, value):
        path = self.folder / name
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(value))
        tmp.replace(path)

    # Rendering ----------------------------------------------------------

    def who(self, ev, state):
        if ev.get("by"):
            return ev["by"]
        info = state.get("workers", {}).get(ev.get("worker"), {})
        return info.get("label") or f"worker {ev.get('worker', '?')}"

    def commit_link(self, sha):
        return f"[`{short(sha)}`]({self.gh.url('commit', sha)})" if self.gh else f"`{short(sha)}`"

    def saved(self, task, ev=None):
        cp = (ev or {}).get("checkpoint") or (None if ev else task.get("checkpoint"))
        if not cp:
            return "No checkpoint was published, so the next agent starts from the task base."
        summary = cp.get("summary") or {}
        files = summary.get("files") or []
        listed = ", ".join(f"`{name}`" for name in files[:6])
        if summary.get("file_count", 0) > 6:
            listed += f" and {summary['file_count'] - 6} more"
        stat = summary.get("stat") or "no file changes"
        line = f"Work saved at {self.commit_link(cp['sha'])} on `{cp['branch']}` ({stat})."
        return line + (f"\nTouched: {listed}." if listed else "")

    def render(self, task, ev, state):
        who = f"**{self.who(ev, state)}**"
        kind = ev["kind"]
        if kind == "imported":
            return f"📥 {who} queued this issue as task `{task['id']}`. {self.audience(task)}"
        if kind == "claimed":
            start = ev.get("start")
            origin = f"the latest checkpoint {self.commit_link(start)}" if start else f"`{task.get('base_branch') or 'base'}` at {self.commit_link(ev.get('base') or task['base'])}"
            note = " The previous lease had expired." if ev.get("recovered") else ""
            return f"🤖 {who} picked this up (attempt {ev['epoch']}), starting from {origin}.{note}"
        if kind == "held":
            return f"🧑‍💻 {who} took this task to work on interactively. Agents will leave it alone until it is handed back."
        if kind == "released":
            return f"↩️ {who} could not check this task out locally, so it is back to **{ev.get('restored')}**."
        if kind == "handoff":
            return (f"⏸ {who} stopped: {ev.get('reason') or 'handed off'}.\n\n{self.saved(task, ev)}\n\n"
                    "Up for grabs: the next available agent continues from this checkpoint.")
        if kind == "queued":
            return (f"⏸ {who} stopped: {ev.get('reason') or 'released'}.\n\n{self.saved(task, ev)}\n\n"
                    "Up for grabs: the next available agent continues from this checkpoint.")
        if kind == "review":
            check = ev.get("check") or "not configured"
            if ev.get("changed_during_check"):
                check += "** (the check changed files; the final snapshot was not re-checked)**"
            pr = ""
            if task.get("pr") and task.get("pr_epoch") == ev.get("epoch"):
                pr = f" {'Draft PR' if task.get('pr_draft', True) else 'PR (drafts are unavailable in this repository)'} #{task['pr']} is open for review."
            return (f"✅ {who} finished attempt {ev.get('epoch', task['epoch'])}. Local check: **{check}**.{pr}\n\n"
                    f"{self.saved(task, ev)}\n\nNothing was merged. A person reviews and merges the PR.")
        if kind == "blocked":
            return (f"⚠️ {who} stopped with an error: {ev.get('reason') or 'unknown'}.\n\n{self.saved(task, ev)}\n\n"
                    "A collaborator can inspect it and comment `/relay retry` with guidance.")
        if kind == "retried":
            note = f"\n\n> {ev['note']}" if ev.get("note") else ""
            return f"🔁 {who} requeued this task.{note}"
        if kind == "note":
            # Notes typed on GitHub are already visible there; mirror only CLI notes.
            return None if ev.get("via") == "github" else f"📝 {who} left a note for the next agent:\n\n> {ev.get('note', '')}"
        if kind == "cancelled":
            return f"🛑 {who} cancelled this task. Running agents stop at their next heartbeat."
        if kind == "done":
            return f"🎉 PR #{ev.get('pr')} was merged. Task `{task['id']}` is done; tasks waiting on it can start."
        return None

    def audience(self, task):
        if task.get("for"):
            names = " or ".join(f"**{name}**'s" for name in task["for"])
            return f"Requested from {names} agent; it starts when that machine is online and has quota."
        return "Any opted-in collaborator's agent can pick it up."

    def issue_body(self, task):
        base = task.get("base_branch") or "base"
        asked = f"{self.audience(task)}\n\n" if task.get("for") else ""
        return (f"{task['prompt']}\n\n{asked}---\n_Agent Relay task `{task['id']}` from `{base}` at `{short(task['base'])}`. "
                "Opted-in agents claim it, checkpoint to `relay/checkpoints/*`, and hand off on usage limits. "
                "Collaborators can steer with `/relay note <text>`, `/relay retry [guidance]` or `/relay cancel`._\n"
                + OWNED.format(task=task["id"]))

    def pr_body(self, task, state):
        attempts = [ev for ev in task["events"] if ev["kind"] in ("claimed", "held")]
        rows = "\n".join(f"| {ev['epoch']} | {self.who(ev, state)} |" for ev in attempts)
        verification = task.get("verification") or {}
        check = verification.get("result", "not configured")
        if verification.get("snapshot_changed_during_check"):
            check += "** on `" + short(verification.get("input_sha")) + "`; the check then changed files, so this head was not re-checked**"
        closes = f"Closes #{task['issue']}\n\n" if task.get("issue") else ""
        return (f"{closes}Produced by coding agents through Agent Relay (task `{task['id']}`).\n\n"
                f"| Attempt | Worker |\n|---|---|\n{rows}\n\nLocal check on the finishing machine: **{check}**. "
                "Checkpoints are unvalidated snapshots; review the full diff before merging.")

    # Posting --------------------------------------------------------------

    def flush(self, task, state=None):
        """Post this worker's unposted events for one task. Never raises."""
        if not self.gh:
            return task
        try:
            state = state or self.store.read()[1]
            return self._flush(task, state)
        except DEFERRABLE as exc:
            self.emit(f"GitHub update deferred: {exc}")
            return task

    def _flush(self, task, state):
        with (self.folder / "conversation.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            posted = set(self._load("posted.json", []))
            self._reconcile_retired(task)  # Safety work first; never gated on narration.
            if task.get("issue") is None:
                if task.get("created_by") != self.worker:
                    return task
                owned = OWNED.format(task=task["id"])
                # Adopt an issue that was created before a crash or timeout instead of opening another.
                existing = next((i["number"] for i in self.gh.labeled_issues(complete=True, state="all") if owned in (i.get("body") or "")), None)
                number = existing or self.gh.create_issue(title(task), self.issue_body(task))
                task = tasks.annotate(self.store, task["id"], issue=number)
                self.emit(f"{'Adopted' if existing else 'Opened'} issue #{task['issue']} for {task['id']}")
            if task["status"] == "review" and task.get("pr") is None and task.get("checkpoint"):
                last = next((ev for ev in reversed(task["events"]) if ev["kind"] == "review"), None)
                if last and last.get("worker") == self.worker and last.get("epoch") == tasks.current_result(task):
                    task = self._open_pr(task, state)
            pending = [ev for ev in task["events"] if ev.get("worker") == self.worker and ev.get("n")
                       and ev["kind"] not in SILENT and f"{task['id']}:{ev['n']}" not in posted]
            if pending:
                # A crash between posting and recording must not post twice: check the thread itself.
                remote = "\n".join(c.get("body") or "" for c in self.gh.issue_comments(task["issue"]))
                for ev in pending:
                    marker = MARKER.format(task=task["id"], n=ev["n"])
                    body = self.render(task, ev, state)
                    if body and marker not in remote:
                        self.gh.comment(task["issue"], body + "\n" + marker)
                    posted.add(f"{task['id']}:{ev['n']}")
                    self._save("posted.json", sorted(posted))
            return task

    def _open_pr(self, task, state):
        cp, epoch = task["checkpoint"], tasks.current_result(task) or task["epoch"]
        try:
            existing = self.gh.find_pr(cp["branch"])  # Created before a crash: adopt, never duplicate.
            if existing:
                number, url, draft = existing["number"], existing.get("html_url"), bool(existing.get("draft"))
            else:
                number, url, draft = self.gh.open_pr(cp["branch"], task.get("base_branch") or "main",
                                                     f"relay: {title(task)}", self.pr_body(task, state))
        except RelayError as exc:
            self.emit(f"Could not open a PR for {task['id']}: {exc}")
            return task
        if not tasks.attach_pr(self.store, task["id"], epoch, number, draft, worker=self.worker):
            # The task moved to a newer attempt while this PR was being created. The refusal put
            # the PR in past_prs; close it through the same isolated reconciliation as any other.
            fresh = self.store.read()[1]["tasks"][task["id"]]
            self._reconcile_retired(fresh)
            return fresh
        self.emit(f"Opened {'draft ' if draft else ''}PR #{number} {url or ''}".rstrip())
        return self.store.read()[1]["tasks"][task["id"]]

    def _reconcile_retired(self, task):
        """Close every superseded PR recorded in shared state (past_prs), each on its own, and
        explain it once. Driven by durable state, not by which events were narrated, so any
        clone that works on the task finishes cleanup another clone left. Each PR is closed
        once: a person who reopens a superseded PR has overridden Relay, and it stays open."""
        closed = set(self._load("retired.json", []))
        noted = set(self._load("retired-notes.json", []))
        for entry in task.get("past_prs", []):
            number, key = entry.get("pr"), f"{task['id']}:{entry.get('pr')}"
            if not number or key in noted:
                continue
            if key not in closed:
                try:
                    self._close_superseded(number, task)
                except DEFERRABLE as exc:
                    self.emit(f"Closing superseded PR #{number} deferred: {exc}")
                    continue
                closed.add(key)
                self._save("retired.json", sorted(closed))
            try:
                self._note_superseded(number, task)
            except Truncated:
                pass  # A thread too long to check for the note: skip the courtesy note.
            except DEFERRABLE as exc:
                self.emit(f"Note on closed PR #{number} deferred: {exc}")
                continue
            noted.add(key)
            self._save("retired-notes.json", sorted(noted))

    def _retire(self, number, task):
        self._close_superseded(number, task)
        self._note_superseded(number, task)

    def _close_superseded(self, number, task):
        """Close a superseded attempt's PR so its `Closes #N` cannot close the live issue."""
        current = self.store.read()[1]["tasks"].get(task["id"]) or {}
        if current.get("pr") == number:
            # Still the current PR (for example during a reversible take): never close it. Once a PR
            # is retired it sits in past_prs and attach_pr refuses it forever, so this cannot flip.
            return
        pull = self.gh.pull(number) or {}
        if pull.get("state") == "open" and not pull.get("merged"):
            self.gh.close_pr(number)

    def _note_superseded(self, number, task):
        """Explain the close on the PR, once, retried on its own marker."""
        current = self.store.read()[1]["tasks"].get(task["id"]) or {}
        pull = self.gh.pull(number) or {}
        if current.get("pr") == number or pull.get("merged") or pull.get("state") == "open":
            return
        note = RETIRED.format(task=task["id"], pr=number)
        if not any(note in (c.get("body") or "") for c in self.gh.issue_comments(number)):
            self.gh.comment(number, f"Superseded by a newer attempt on task `{task['id']}`; closed. "
                                    f"The branch stays available for reference.\n{note}")

    def flush_all(self, state):
        for task in state["tasks"].values():
            if any(ev.get("worker") == self.worker for ev in task["events"]) or (
                    task.get("issue") is None and task.get("created_by") == self.worker):
                self.flush(task, state)

    # Reading GitHub back ----------------------------------------------------

    def sync(self, default_branch):
        """Import labelled issues, apply collaborator commands, mark merged PRs done."""
        if not self.gh:
            return {"github": False}
        report = {"imported": [], "commands": [], "done": []}
        _, state = self.store.read()
        linked = {t.get("issue") for t in state["tasks"].values()}
        by = self.cfg.get("label")
        for issue in self.gh.labeled_issues():
            # Issues Relay opened for its own tasks carry an ownership marker; never import those.
            if issue["number"] in linked or "<!-- relay-task:" in (issue.get("body") or ""):
                continue
            if not self.gh.can_write(issue["user"]["login"]):
                continue
            refs = output(self.repo, "ls-remote", "--refs", self.cfg["remote"], f"refs/heads/{default_branch}")
            if not refs:
                raise RelayError(f"Default branch {default_branch!r} is not on the remote")
            prompt = issue["title"] + ("\n\n" + issue["body"] if issue.get("body") else "")
            assignees = [person["login"] for person in issue.get("assignees") or []]
            task_id = tasks.import_issue(self.store, issue["number"], prompt, refs.split()[0],
                                         default_branch, self.worker, by,
                                         requested_by=issue["user"]["login"], for_=assignees)
            if task_id:
                report["imported"].append(issue["number"])
        report["commands"] = self._commands()
        _, state = self.store.read()
        for task in state["tasks"].values():
            if task["status"] in ("review", "held") and task.get("pr"):
                if (self.gh.pull(task["pr"]) or {}).get("merged"):
                    if tasks.mark_done(self.store, task["id"], task["pr"], worker=self.worker):
                        report["done"].append(task["id"])
        self.flush_all(self.store.read()[1])
        return report

    def _commands(self):
        cursor = self._load("sync.json", {})
        since = cursor.get("since") or self.cfg.get("enrolled_at") or iso(time.time() - 3600)
        _, state = self.store.read()
        by_number = {}
        for task in state["tasks"].values():
            for key in ("issue", "pr"):
                if task.get(key):
                    by_number[task[key]] = task["id"]
        applied = []
        newest = since
        for comment in self.gh.comments_since(since):
            newest = max(newest, comment.get("updated_at") or comment.get("created_at") or since)
            words = (comment.get("body") or "").strip().split(None, 2)
            if len(words) < 2 or words[0] != "/relay" or words[1] not in VERBS:
                continue
            number = int(comment["issue_url"].rstrip("/").rsplit("/", 1)[1])
            task_id = by_number.get(number)
            login = comment["user"]["login"]
            if not task_id or not self.gh.can_write(login):
                continue
            text = words[2].strip() if len(words) > 2 else ""
            result = tasks.command(self.store, comment["id"], task_id, words[1], text, self.worker, login)
            if result and result != "ignored":
                applied.append({"task": task_id, "verb": words[1], "by": login, "result": result})
                try:
                    self.gh.react(comment["id"])
                except RelayError:
                    pass
        self._save("sync.json", {"since": newest})
        return applied


def iso(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

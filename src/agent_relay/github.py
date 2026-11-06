"""GitHub REST calls through the owner's own `gh` login. Best effort, never authoritative."""

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from urllib.parse import urlencode

from .gitstore import RelayError, output

SLUG = re.compile(r"github\.com[:/]+([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
WRITERS = ("admin", "maintain", "write")
PERMISSION_TTL = 600
MAX_PAGES = 20


def gh_argv():
    custom = os.environ.get("RELAY_GH")
    if custom:
        return shlex.split(custom)
    found = shutil.which("gh")
    return [found] if found else None


def detect(repo, remote):
    """Return 'owner/name' when the remote is on github.com and `gh` is signed in."""
    match = SLUG.search(output(repo, "remote", "get-url", remote))
    argv = gh_argv()
    if not match or not argv:
        return None
    try:
        probe = subprocess.run([*argv, "auth", "status"], capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return None
    return f"{match[1]}/{match[2]}" if probe.returncode == 0 else None


class GitHub:
    def __init__(self, slug, argv=None):
        self.slug = slug
        self.argv = argv or gh_argv()
        if not self.argv:
            raise RelayError("GitHub mirroring needs the `gh` CLI; install it and run `gh auth login`")
        self._permissions = {}
        self.timeout = 60

    def api(self, method, path, body=None, **params):
        if params:
            path = f"{path}?{urlencode(params)}"
        argv = [*self.argv, "api", "--method", method, path]
        data = None
        if body is not None:
            argv += ["--input", "-"]
            data = json.dumps(body).encode()
        try:
            result = subprocess.run(argv, input=data, capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise RelayError(f"GitHub {method} {path.split('?')[0]} timed out") from exc
        if result.returncode:
            message = result.stderr.decode(errors="replace").strip() or result.stdout.decode(errors="replace").strip()
            raise RelayError(f"GitHub {method} {path.split('?')[0]} failed: {message[:300]}")
        text = result.stdout.decode().strip()
        return json.loads(text) if text else None

    def pages(self, path, complete=False, **params):
        """Follow page numbers until a short page; bounded so a huge repo cannot stall a worker.
        With complete=True, hitting the bound raises instead of returning a partial list, for
        duplicate checks that must see everything."""
        for page in range(1, MAX_PAGES + 1):
            items = self.api("GET", path, **params, per_page=100, page=page) or []
            yield from items
            if len(items) < 100:
                return
        if complete:
            raise RelayError(f"More than {MAX_PAGES * 100} items at {path}; not risking a duplicate")

    def repo(self, suffix=""):
        return f"repos/{self.slug}{suffix}"

    def login(self):
        return self.api("GET", "user")["login"]

    def ensure_label(self, name="relay"):
        try:
            self.api("POST", self.repo("/labels"), {"name": name, "color": "5319e7",
                                                    "description": "Work for the Agent Relay pool"})
        except RelayError as exc:
            if "already_exists" not in str(exc) and "422" not in str(exc):
                raise

    def create_issue(self, title, body, labels=("relay",)):
        return self.api("POST", self.repo("/issues"), {"title": title, "body": body, "labels": list(labels)})["number"]

    def comment(self, number, body):
        return self.api("POST", self.repo(f"/issues/{number}/comments"), {"body": body})

    def react(self, comment_id, content="+1"):
        return self.api("POST", self.repo(f"/issues/comments/{comment_id}/reactions"), {"content": content})

    def open_pr(self, head, base, title, body, draft=True):
        request = {"head": head, "base": base, "title": title, "body": body, "draft": draft}
        try:
            pr = self.api("POST", self.repo("/pulls"), request)
        except RelayError as exc:
            # GitHub Free rejects drafts on private repositories with this exact message. The
            # fallback PR is still unmerged and the narration says it is not a draft.
            if not draft or "draft pull requests are not supported" not in str(exc).lower():
                raise
            pr = self.api("POST", self.repo("/pulls"), {**request, "draft": False})
        return pr["number"], pr.get("html_url"), bool(pr.get("draft"))

    def find_pr(self, branch):
        """An existing PR from this head branch, e.g. one created before a crash."""
        owner = self.slug.split("/")[0]
        found = self.api("GET", self.repo("/pulls"), head=f"{owner}:{branch}", state="all")
        # A PR someone closed without merging is not adopted; a fresh one is opened instead.
        usable = [p for p in found or [] if p.get("state") == "open" or p.get("merged_at") or p.get("merged")]
        return usable[0] if usable else None

    def close_pr(self, number):
        return self.api("PATCH", self.repo(f"/pulls/{number}"), {"state": "closed"})

    def pull(self, number):
        return self.api("GET", self.repo(f"/pulls/{number}"))

    def labeled_issues(self, label="relay", complete=False):
        issues = self.pages(self.repo("/issues"), complete=complete, labels=label, state="open")
        return [issue for issue in issues if "pull_request" not in issue]

    def comments_since(self, since):
        # Sorted by update time, the same clock as `since`, so the cursor never skips a page.
        return list(self.pages(self.repo("/issues/comments"), since=since, sort="updated", direction="asc"))

    def issue_comments(self, number):
        return list(self.pages(self.repo(f"/issues/{number}/comments"), complete=True))

    def can_write(self, login):
        """Cached for ten minutes so revoked access stops working; lookup failures are not cached."""
        cached = self._permissions.get(login)
        if cached and time.monotonic() - cached[1] < PERMISSION_TTL:
            return cached[0] in WRITERS
        try:
            value = self.api("GET", self.repo(f"/collaborators/{login}/permission"))["permission"]
        except RelayError as exc:
            if "404" not in str(exc):
                raise
            value = "none"  # Unknown or deleted account.
        self._permissions[login] = (value, time.monotonic())
        return value in WRITERS

    def url(self, kind, ref):
        return f"https://github.com/{self.slug}/{kind}/{ref}"

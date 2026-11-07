"""Snapshot a quiesced, isolated worktree without touching its index or HEAD."""

import os
import re
import tempfile
from pathlib import Path

from .gitstore import RelayError, git, output

SENSITIVE_NAME = re.compile(
    r"(^|/)(\.env(?:\..*)?|\.aws|\.ssh|\.npmrc|\.pypirc|credentials(?:\..*)?|"
    r"id_(?:rsa|ed25519)|.*\.(?:pem|key|p12|pfx))($|/)", re.I,
)
SECRET = re.compile(
    rb"-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----|"
    rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|"
    rb"sk-ant-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b"
)
MAX_FILE = 5 * 1024 * 1024


def scan_tree(repo, tree):
    """Inspect staged blobs, so the actual published snapshot is what gets scanned."""
    entries = git(repo, "ls-tree", "-r", "-z", tree).stdout.split(b"\0")
    total = 0
    for entry in entries:
        if not entry:
            continue
        meta, raw_name = entry.split(b"\t", 1)
        mode, kind, sha = meta.decode().split()
        name = raw_name.decode(errors="replace")
        if SENSITIVE_NAME.search(name):
            raise RelayError(f"Checkpoint blocked: sensitive filename {name!r}")
        if mode in ("120000", "160000"):
            raise RelayError(f"Checkpoint blocked: symlink/submodule {name!r} is unsupported in this alpha")
        size = int(output(repo, "cat-file", "-s", sha))
        total += size
        if size > MAX_FILE or total > 50 * MAX_FILE:
            raise RelayError("Checkpoint blocked: snapshot exceeds the alpha size limit")
        if SECRET.search(git(repo, "cat-file", "blob", sha).stdout):
            raise RelayError(f"Checkpoint blocked: possible credential in {name!r}")


def snapshot(worktree, parent, task_id, epoch, label="unvalidated checkpoint"):
    fd, index = tempfile.mkstemp(prefix="relay-index-")
    os.close(fd)
    os.unlink(index)
    env = {"GIT_INDEX_FILE": index}
    try:
        git(worktree, "read-tree", parent, env=env)
        excludes = [f":(glob,exclude)**/{folder}/**" for folder in
                    ("__pycache__", ".venv", "node_modules", ".pytest_cache")]
        git(worktree, "add", "-A", "--", ".", *excludes, env=env)
        tree = output(worktree, "write-tree", env=env)
        scan_tree(worktree, tree)
        if tree == output(worktree, "rev-parse", f"{parent}^{{tree}}"):
            return parent
        return output(worktree, "commit-tree", tree, "-p", parent, "-m",
                      f"relay: {label} {task_id} attempt {epoch}")
    finally:
        Path(index).unlink(missing_ok=True)
        Path(index + ".lock").unlink(missing_ok=True)


def publish(repo, remote, sha, task_id, epoch):
    branch = f"relay/checkpoints/{task_id}/{epoch}"
    git(repo, "push", "--quiet", remote, f"{sha}:refs/heads/{branch}")
    return {"sha": sha, "branch": branch, "validated": False}


def summarize(repo, base, sha):
    """What changed relative to the task base, computed by Git rather than by a model."""
    names = output(repo, "diff", "--name-only", base, sha).splitlines()
    return {"files": names[:20], "file_count": len(names),
            "stat": output(repo, "diff", "--shortstat", base, sha)}

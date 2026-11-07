import json
import fcntl
import os
import tempfile
import time
import uuid

from .gitstore import RelayError, private_dir


def load(repo):
    path = private_dir(repo) / "config.json"
    if not path.exists():
        raise RelayError("Run `relay init` in this clone first")
    return json.loads(path.read_text())


def save(repo, config):
    folder = private_dir(repo)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=folder, prefix="config-")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(config, handle, indent=2)
            handle.write("\n")
        os.replace(name, folder / "config.json")
    finally:
        if os.path.exists(name):
            os.unlink(name)


def initialize(repo, remote, check, **fields):
    try:
        existing = load(repo)
    except RelayError:
        existing = {"worker": uuid.uuid4().hex[:12], "mode": "paused",
                    "enrolled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    existing.update(remote=remote, check=check, **fields)
    save(repo, existing)
    return existing


def update(repo, **fields):
    """Serialize owner mode changes with worker cooldown writes."""
    with (private_dir(repo) / "config.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        cfg = load(repo)
        cfg.update(fields)
        save(repo, cfg)
        return cfg

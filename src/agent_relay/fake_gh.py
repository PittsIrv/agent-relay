"""Offline stand-in for `gh api`, used by the demo and tests. Never contacts GitHub.

State lives in the JSON file named by FAKE_GH_STATE; the acting login is FAKE_GH_LOGIN.
"""
import fcntl
import json
import os
import random
import re
import sys
import time
from urllib.parse import parse_qs, urlsplit


def load(handle):
    handle.seek(0)
    text = handle.read()
    return json.loads(text) if text else {"next": 1, "issues": {}, "comments": [], "pulls": {},
                                          "labels": [], "permissions": {}, "reactions": []}


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def main(argv):
    if argv[:2] == ["auth", "status"]:
        return 0
    if argv[0] != "api":
        print(f"fake gh: unsupported {argv}", file=sys.stderr)
        return 1
    method, path = "GET", None
    rest = argv[1:]
    body = None
    while rest:
        item = rest.pop(0)
        if item == "--method":
            method = rest.pop(0)
        elif item == "--input":
            rest.pop(0)
            body = json.loads(sys.stdin.read() or "null")
        else:
            path = item
    url = urlsplit(path)
    query = {k: v[0] for k, v in parse_qs(url.query).items()}
    login = os.environ.get("FAKE_GH_LOGIN", "octo")
    # Network latency is spent before the state lock, as a real round trip would be.
    time.sleep(random.uniform(0, float(os.environ.get("FAKE_GH_LATENCY", "0"))))
    with open(os.environ["FAKE_GH_STATE"], "a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        db = load(handle)
        status, result = route(db, method, url.path, query, body, login)
        if isinstance(result, list) and "per_page" in query:
            size, page = int(query["per_page"]), int(query.get("page", 1))
            result = result[(page - 1) * size: page * size]
        handle.seek(0)
        handle.truncate()
        json.dump(db, handle, indent=2)
    if status >= 400:
        print(f"gh: {result} (HTTP {status})", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


def route(db, method, path, query, body, login):
    if path == "user":
        return 200, {"login": login}
    match = re.fullmatch(r"repos/([^/]+/[^/]+)(/.*)?", path)
    if not match:
        return 404, "Not Found"
    slug, sub = match[1], match[2] or ""
    if sub == "/labels" and method == "POST":
        if body["name"] in db["labels"]:
            return 422, "already_exists"
        db["labels"].append(body["name"])
        return 201, {"name": body["name"]}
    if sub == "/issues" and method == "POST":
        number = db["next"]
        db["next"] += 1
        issue = {"number": number, "title": body["title"], "body": body.get("body"), "state": "open",
                 "labels": [{"name": n} for n in body.get("labels", [])], "user": {"login": login},
                 "html_url": f"https://github.com/{slug}/issues/{number}"}
        db["issues"][str(number)] = issue
        return 201, issue
    if sub == "/issues" and method == "GET":
        label = query.get("labels")
        wanted = query.get("state", "open")
        return 200, [i for i in db["issues"].values() if (wanted == "all" or i["state"] == wanted)
                     and (not label or any(l["name"] == label for l in i["labels"]))]
    found = re.fullmatch(r"/issues/(\d+)/comments", sub)
    if found and method == "POST":
        comment = {"id": len(db["comments"]) + 1, "body": body["body"], "user": {"login": login},
                   "created_at": now(), "updated_at": now(),
                   "issue_url": f"https://api.github.com/repos/{slug}/issues/{found[1]}"}
        db["comments"].append(comment)
        return 201, comment
    if found and method == "GET":
        return 200, [c for c in db["comments"] if c["issue_url"].endswith(f"/issues/{found[1]}")]
    if sub == "/issues/comments" and method == "GET":
        since = query.get("since", "")
        return 200, [c for c in db["comments"] if c["updated_at"] >= since]
    found = re.fullmatch(r"/issues/comments/(\d+)/reactions", sub)
    if found:
        db["reactions"].append({"comment": int(found[1]), "content": body["content"], "user": login})
        return 201, {"content": body["content"]}
    if sub == "/pulls" and method == "POST":
        number = db["next"]
        db["next"] += 1
        pull = {"number": number, "title": body["title"], "body": body["body"], "head": body["head"],
                "base": body["base"], "draft": body.get("draft", False), "state": "open", "merged": False,
                "html_url": f"https://github.com/{slug}/pull/{number}"}
        db["pulls"][str(number)] = pull
        return 201, pull
    if sub == "/pulls" and method == "GET":
        head = query.get("head", "").split(":", 1)[-1]
        return 200, [p for p in db["pulls"].values() if not head or p["head"] == head]
    found = re.fullmatch(r"/pulls/(\d+)", sub)
    if found:
        pull = db["pulls"].get(found[1])
        if pull and method == "PATCH":
            pull.update(body)
        return (200, pull) if pull else (404, "Not Found")
    found = re.fullmatch(r"/collaborators/([^/]+)/permission", sub)
    if found:
        return 200, {"permission": db["permissions"].get(found[1], "read")}
    return 404, f"fake gh has no route for {method} {path}"


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

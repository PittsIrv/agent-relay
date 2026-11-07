## Agent Relay 0.3.0 (alpha)

Hand unfinished coding work to a collaborator's agent through GitHub. This release proves the
handoff over real GitHub and makes the first run work from the README alone.

### What it does

You hit a usage limit mid-task. Relay saves your in-progress work, posts it as a GitHub issue, and
a collaborator's agent continues from your exact files and opens a draft PR. Nobody shares an
account and nothing merges on its own.

- `relay take` / `relay handoff`: work on a task yourself, then hand it back.
- Retry a reviewed task: the new attempt gets its own PR; the old one is closed with a note.
- Cancel from the terminal or with `/relay cancel`. Cancelling after review closes the PR (branch
  kept; reopen to undo) and is allowed only to the person who requested the task.
- A Claude Code hook hands your session off automatically when it hits a usage limit (experimental: see Known limits).

### What is shared

Your uncommitted and untracked files are saved as a checkpoint branch on the repository's remote, and the
task text becomes a GitHub issue, so both are visible to everyone with access to that repository. Use it
only on repositories you are willing to share with your collaborators. A heuristic guard refuses to hand
off obvious secret files; it is not a scanner. Agent logs, accounts and credentials stay on each machine.

### Try it in two minutes

You need Python 3.11+, Git and, for a real repository, the GitHub CLI (`gh auth login`) plus a signed-in
`claude` or `codex`. The demo needs only Python and Git.

```sh
python3 -m venv ~/.relay && ~/.relay/bin/pip install 'git+https://github.com/PittsIrv/agent-relay.git'
export PATH="$HOME/.relay/bin:$PATH"
relay demo
```

`pipx install 'git+https://github.com/PittsIrv/agent-relay.git'` works too. For a real repository, see
the README's "Set up with a friend".

### Verified

45 end-to-end checks pass over real GitHub between two clones (take 7, hand back 9, supersede 5,
cancel 16, usage-limit handoff 8). CI is green on macOS and Linux, Python 3.11 to 3.13. A new user
following only the README reaches a first handoff in about half a minute of commands.

### Known limits

- The end-to-end tests use one shared GitHub login; owners differ by Relay identity only.
  Scenarios that need two logins are covered by the offline fake-`gh` suite only.
- The real usage-limit hook is experimental and untested; it runs against a synthetic payload until a real
  rate-limit event is observed.
- Real provider limits and multi-device use have not been observed in the field.
- Cancel after review is owner-only; there is no maintainer override yet.
- macOS and Linux only. See the README for the rest.

Full list of changes: [CHANGELOG](CHANGELOG.md).

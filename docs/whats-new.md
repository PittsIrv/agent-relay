# What's new

## 2026-11-07 · v0.3: proven over real GitHub

**Try:** follow the README from `pipx install …` (or the venv line) to `relay take` and
`relay handoff` in a fresh clone; it takes about a minute.

**You get:** the handoff scenarios (take, hand back, supersede, cancel, usage-limit) are
checked over real GitHub between two clones; cancelling a reviewed task closes its PR; the
README and error messages guide a first-time user.

## 2026-11-06 · v0.2: hand work to a friend's agent through GitHub

**Try:** `relay demo`, then read the issue thread it prints. In a real repo:
`relay init`, `relay hook install`, and ask a friend to run `relay start`.

**You get:** your Claude Code session hands off by itself when it hits a usage
limit; the work shows up as a GitHub issue that a collaborator's agent picks up,
continues and turns into a draft PR. You can address one friend's agent (assign
the issue or `--for`), steer with `/relay` comments, and each owner limits whose
work their agent accepts and how much per day. Verified live with Codex as the
successor.

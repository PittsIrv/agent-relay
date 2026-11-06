# Architecture and guarantees

Agent Relay is a deterministic supervisor for existing CLIs, not another LLM
runtime. The scope is small, trusted groups working on a shared GitHub repo, where
each person lends their own agent with their own consent.

## Three layers

| Layer | Where | Authority |
|---|---|---|
| Coordination | `relay/state` branch, one JSON document | Authoritative. Compare-and-swap pushes. |
| Code | `relay/checkpoints/<task>/<attempt>` branches | Immutable-history snapshots, never merged automatically. |
| Conversation | GitHub issues, comments, reactions, draft PRs (through `gh`) | Mirror only. Best effort, reconciled. Never decides ownership. |

## Why Git holds coordination

A separate server would add hosting and authentication to a two-person workflow.
Every mutation of `relay/state` reads a commit, creates a child, and pushes with
`--force-with-lease=refs/heads/relay/state:<expected SHA>`. Conflicting mutations
reread and retry; auth, protection and outage errors surface instead of retrying.
GitHub issues and comments alone cannot give this atomic claim.

This trades scale for setup simplicity. Heartbeats produce commits and network
traffic; it is not for large fleets. Workers poll and the oldest eligible task wins.
"Eligible" means: queued; dependencies (`--after`) merged; addressed to this owner
if addressed at all (`--for`, issue assignees); requested by someone the owner
accepts (`--accept-from`); within the owner's daily cap. Different tasks run in
different clones concurrently; integration stays with people through PR review.

## Task lifecycle

`queued -> running -> review -> done`, plus `held`, `blocked`, `cancelled`.

Recognized usage limits and owner pauses return a task to `queued`. Unexpected
agent or supervisor failures become `blocked`, with the CLI's structured error code
(never its message text). `retry` requeues `blocked`, `review` or `cancelled`
tasks. An expired running lease is reclaimable; a held one is not. `cancel` drops
the lease, so a running worker stops at its next heartbeat and keeps its files
locally. `review` is not a claim of successful implementation.

Each claim increments an epoch. Updates must match worker ID, epoch, and an
unexpired lease. After reassignment an old worker cannot overwrite canonical task
state. It may already have pushed to its own attempt branch; that branch is never
merged or selected automatically.

Leases need reasonably synchronized clocks. Skew can cause premature takeover and
duplicate model execution. There is no exactly-once execution and no protection
against a malicious collaborator rewriting metadata. A failed heartbeat makes a
live supervisor stop its process group. Killing only the supervisor with SIGKILL
can leave children running. Host death stops its processes but loses unsynced work.

## The GitHub conversation

- **Issues.** The task's creator opens one issue per task, carrying a hidden
  ownership marker so no worker imports it as a second task. Issues labelled
  `relay` and opened by a writer (admin, maintain, write) are imported as tasks;
  import is idempotent under CAS and paginated. Assignees become the addressees.
- **Comments.** Each meaningful event (claim, handoff, review, block, retry,
  cancel, done, CLI note) is posted by the worker that caused it, with a hidden
  `<!-- relay:task:n -->` marker. Before posting, the worker checks the thread for
  the marker, so a lost local cache does not duplicate; two concurrent posts of the
  same event remain possible only if one worker runs twice at once. Each event
  records the checkpoint and check result of its own moment, so late comments are
  accurate. If GitHub is down the comment is posted on a later flush; checkpoints
  and leases never wait for it. Events older than the last 50 per task are dropped
  from state and are no longer narrated.
- **Commands.** `/relay note TEXT`, `/relay retry [TEXT]`, `/relay cancel` on the
  issue or PR, from writers only (permission cached for ten minutes). Each comment
  id is applied at most once while it is among the last 5,000 handled ids in shared
  state; the applying worker reacts 👍. Workers sync every minute, also while an
  agent is running, so a cancel lands within about a minute plus a heartbeat. Notes reach the next
  agent's prompt. Issue bodies other than the task text and ordinary comments do
  not, because repository text is untrusted input.
- **PRs.** The worker that finishes opens a draft PR from its checkpoint branch
  with `Closes #N`. Where GitHub rejects drafts ("Draft pull requests are not
  supported"), it opens a normal unmerged PR and says so in the comment. PRs belong
  to an attempt: retrying or taking a task retires its PR, and only the current
  attempt's PR can mark the task `done` when merged. A `--after` dependency counts
  as done when its PR merges, wherever that PR's base branch is.
- **Identity.** Comments run under each owner's own `gh` login. People are named in
  bold, never `@mentioned`.

## Interactive handoff

`relay hook install` adds a project-local Claude Code hook (in the Git-excluded
`.claude/settings.local.json`) on `StopFailure` with matcher `rate_limit`. When the
interactive session is cut off, the hook runs `relay handoff --from-hook`, which:

1. snapshots the owner's working tree through a temporary index (index, HEAD,
   branch and files unchanged; unpushed commits travel along),
2. refuses if the credential guard trips (reason logged to `.git/agent-relay/hook.log`),
3. creates or reuses a task (a second firing with the same tree is a no-op),
4. pushes the checkpoint, queues the task and narrates it.

The successor's goal comes from the owner's `relay handoff` message, an earlier
`relay take`, recent commit messages and `.relay-handoff.md`. The last user prompt
is shared only after `relay hook install --share-prompt`.

`relay take` puts a task in `held` for a person: workers skip it and its lease does
not expire. It fetches first and returns the task to the queue if the local
checkout fails. `relay handoff` (or `--review`) gives it back; anyone can free a
forgotten hold with `relay retry` after `relay cancel`.

## Checkpoints

The supervisor SIGSTOPs the agent process group, stages into a separate temporary
Git index, scans the staged blobs, creates a commit, and pushes the attempt branch.
It then records the pushed SHA and a Git-computed diff summary under its current
lease. Its own index and detached HEAD are unaffected. On normal exit remaining
child processes are terminated before the final checkpoint.

Freezing processes does not make an interrupted multi-file edit consistent.
Checkpoints are always unvalidated. Detached or escaped child processes and
malicious agents are outside this cooperative process-group model.

The successor starts at the latest recorded checkpoint and receives the task, a
supervisor-recorded history (attempts, why each stopped, files changed),
collaborator notes and `.relay-handoff.md`. Logs and private transcripts never
cross owners. Packages, ignored files, local databases and credentials do not move.

Verification uses only the receiving owner's locally configured argv. Its exit
code and input snapshot are recorded. If the check changes files, a further
checkpoint is saved and `snapshot_changed_during_check` is true.

## Storage and boundaries

- `refs/heads/relay/state`: task prompts, leases, last 50 events per task, notes,
  worker presence, handled comment ids and checkpoint pointers.
- `refs/heads/relay/checkpoints/<task>/<epoch>`: attempt branches.
- `.git/agent-relay/config.json`: worker identity, owner, opt-in mode, consent
  policy, local check, cooldown.
- `.git/agent-relay/posted.json`, `sync.json`, `hook.log`: conversation cache,
  comment cursor, hook outcomes.
- `.git/agent-relay/attempts/`: retained worktrees, private logs, prompts, recovery SHAs.

No automatic pruning yet. Repository administrators must allow direct writes to
`relay/*` branches while keeping normal protection on the main branch.

## Next validation milestones

1. Two real devices over a real GitHub repository, with a provider-observed usage
   limit firing the hook.
2. Measure handoff latency (limit to successor's first checkpoint) against the
   manual baseline of messaging a friend.
3. Offline recovery, skewed clocks, process-group escape and interrupted Git tests.
4. Onboarding study with two-person teams.
5. Only then: reset-time telemetry, account-wide reservation, smarter scheduling.

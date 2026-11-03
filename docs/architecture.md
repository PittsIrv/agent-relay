# Architecture and guarantees

Agent Relay is a deterministic supervisor for existing CLIs, not another LLM
runtime. The initial scope is small, trusted groups working on a shared Git repo.

## Why Git coordinates the alpha

A separate server would add installation, hosting and authentication to the
two-person workflow. A dedicated `relay/state` branch holds one JSON document.
Every mutation reads a commit, creates a child, and pushes with an exact
`--force-with-lease=refs/heads/relay/state:<expected SHA>` comparison. Conflicting
mutations reread and retry. Initial creation also uses an expected empty ref.
GitHub issues/comments alone would not give this atomic claim operation.

This trades scalability for setup simplicity. Heartbeats produce commits and Git
network traffic. It is not appropriate for large fleets. There is no event-driven
wakeup, fairness scheduler, dependency graph, or GitHub issue/PR integration yet.
The first release polls and assigns the oldest eligible task. Different tasks can
run in different clones concurrently; integration remains a human responsibility.

## Task lifecycle

`queued -> running -> review`

Recognized quota interruptions and explicit owner pauses return a task to `queued`.
Unexpected agent or supervisor failures become `blocked` when the lease/network
still permits that update. `retry` requeues `blocked` or `review` tasks. An expired
running lease is reclaimable. `review` is not a claim of successful implementation.

Each claim increments an epoch. Updates must match worker ID, epoch, and an
unexpired lease. After reassignment, an old worker cannot overwrite canonical
task state through the supervisor. It may have already pushed to its own attempt
branch; that branch is never automatically merged or selected as canonical.

Leases require reasonably synchronized clocks. Clock skew can cause premature
takeover and duplicate model execution. This system does not guarantee exactly-once
execution or prevent a malicious peer from rewriting metadata. CAS protects shared
state; it does not cancel a partitioned or orphaned model process. A failed heartbeat
causes a live supervisor to stop its process group. Killing only the supervisor
with SIGKILL can leave children running: use graceful shutdown or terminate the
whole process group. Full host death stops its processes but loses unsynced work.

## Checkpoints

The daemon SIGSTOPs the agent process group, stages into a separate temporary Git
index, scans the staged blobs, creates a commit, and pushes an attempt branch.
It then updates metadata with the pushed SHA under its current lease. Its own
index and detached HEAD are unaffected. On normal exit, remaining child processes
are terminated before the final checkpoint.

Freezing processes does not make an interrupted multi-file edit semantically
consistent. Checkpoints always remain unvalidated. Detached/escaped child
processes and malicious agents are outside this cooperative process-group model.

The successor begins at the latest recorded checkpoint, receives the task contract
and inspects code/tests plus optional `.relay-handoff.md`. Logs and private session
transcripts are never used as a cross-owner transport. Environment packages,
ignored files, local databases and credentials do not move with the checkpoint.

Verification uses only the receiving owner's locally configured argv. Its exit
code and input snapshot are recorded. If the check changes files, a further
checkpoint is saved and `snapshot_changed_during_check` is true. A passing check
therefore must not be assumed to validate a changed final tree.

## Storage and boundaries

- `refs/heads/relay/state`: task prompts, leases, last 50 events/task and checkpoint pointers.
- `refs/heads/relay/checkpoints/<task>/<epoch>`: immutable-history attempt branches.
- `.git/agent-relay/config.json`: worker identity, opt-in mode, local check, cooldown.
- `.git/agent-relay/attempts/`: retained worktrees, private logs, prompts, recovery SHAs.

No automatic pruning yet. Both Git history and local attempts grow over time.
Do not remove an attempt until its work has been reviewed and safely retained.
Repository administrators must permit direct writes to `relay/*` branches while
keeping normal protections on the project's main branch. Repositories with hooks
or rulesets rejecting these branches need configuration before use.

## Next validation milestones

1. Two real devices, same provider, real structured limit event, manual review.
2. Cross-provider continuation with reproducible environment setup.
3. Offline recovery, skewed clocks, process-group escape and interrupted Git tests.
4. Simple install/onboarding study with two-person teams.
5. Only then: reset-time telemetry, account-wide reservation, task dependencies,
   automatic PR creation, and measured scheduling improvements.

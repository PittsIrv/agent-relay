# Validation scope

## Automated (every CI run)

42 tests on temporary bare Git remotes, independent clones, real child processes,
fake agents and an offline `gh` stand-in (`agent_relay/fake_gh.py`). They never
spend provider quota or write to GitHub.

Coordination and recovery:

- Interrupted fake agent leaves uncommitted edits; another clone fetches and completes them.
- Competing claims yield one lease owner; expired workers cannot overwrite a successor.
- Pausing preserves partial work and releases the task; paused workers do not claim.
- Sensitive files block publication and stay local; caches stay out of snapshots.
- Failed pushes retain local recovery coordinates without advertising them.
- Authentication-style failures block instead of retrying; ordinary prose mentioning
  "rate limit" is not a limit; the structured shape of a live Claude Code auth
  failure is classified correctly.

Interactive handoff:

- The `StopFailure(rate_limit)` hook publishes uncommitted work with the owner's
  status and HEAD unchanged, and another clone finishes it.
- Other error types and clean trees do nothing; repeated firing reuses one task;
  a credentials file blocks the hook and the reason is logged locally.
- `relay take` holds a task (never expires, workers skip it), refuses a dirty
  checkout, and `relay handoff` returns it with a new attempt and note.
- Hook install merges with existing settings, is idempotent, and is Git-excluded.

GitHub conversation:

- Issue, comment sequence and draft PR (`Closes #N`) for a two-attempt handoff;
  re-flushing from both clones posts no duplicates; no `@` mentions.
- Issue import requires write access and is idempotent across clones.
- `/relay retry` from a writer applies once and reaches the next prompt; the same
  command from a read-only user is ignored.
- `/relay cancel` stops a running agent at its next heartbeat; files stay local.
- A merged PR marks the task done and unblocks `--after` dependents.
- A GitHub outage does not block a handoff; deferred comments post later.
- Assigned issues go only to the assignee's agent; `--accept-from` and the daily
  cap hold.

Regressions for the 2026-11-06 independent review:

- A retried task opens a new PR for its new attempt; merging the old attempt's PR
  does not complete it.
- An issue Relay opened for its own task is never imported as a second task, even
  before its number is recorded.
- `/relay cancel` reaches a running agent through its own worker's sync.
- Commands after 130 ordinary comments (beyond the first API page) are applied.
- Revoked write access stops being honoured once the permission cache expires.
- Losing the local "posted" cache does not duplicate comments.
- A hanging `gh` call times out without blocking task creation.
- A failed `relay take` returns the task to the queue instead of stranding it.
- The usage-limit hook publishes new work after the previous task was completed.

Regressions for the re-review of those fixes:

- A stale, late PR creation for an old attempt is closed as superseded and never
  linked to the newer attempt; merging it does not complete the task.
- Retrying a task in review closes its superseded PR on GitHub.
- A `gh` call slower than the lease never costs a running worker its lease.
- An issue created just before a crash is adopted, not duplicated.

`relay demo` runs the headline scenario end to end.

## Live runs (2026-11-06, macOS, local bare remote, offline GitHub stand-in)

| Successor | Version | Outcome |
|---|---|---|
| Claude Code (`--model haiku`) | 2.1.132 | Blocked in 10 s: the standalone CLI's login had expired (`authentication_failed`, HTTP 401). Classified as an error, not a limit; no retry loop; work preserved. |
| Codex (ChatGPT login, default model) | 0.144.3 | Kept the predecessor's `mean()`, implemented `median()`; the owner's check (`unittest`) passed; draft PR opened. 34 s from claim to review. |

The predecessor in both runs was a simulated interactive session: an uncommitted
edit plus a `StopFailure` payload in the documented shape. The run script and its
evidence are kept outside the repository.

## Not yet validated

- A provider-observed usage limit firing the real hook.
- Two physical machines over a real GitHub repository.
- Universal secret detection, skewed clocks, partitioned workers in the field.

Before calling a provider adapter validated, record CLI version, platform, auth
mode (never credentials), sanitized structured error type, remote checkpoint SHA,
successor start, verification result and owner pause behavior. Keep private
transcripts out of public reports.

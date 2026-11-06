# 0002: GitHub as the conversation, interactive handoff, small scheduler

Date: 2026-11-06. Status: accepted. Who decided: Claude, under the owner's
explicit delegation ("you can decide and develop it"), except D12's use case, which
the owner stated.

## Intent

Make Agent Relay publishable around its real use case: collaborators lend their
idle agents to each other, with consent, and GitHub is where people and agents
see and steer the work. A usage limit in your own Claude Code session should hand
off without you.

## Context

The v0.1 alpha moves tasks between background workers through a hidden
`relay/state` Git branch. Two gaps remained against the original use case:

1. The motivating scenario is a person working *interactively* in Claude Code
   who hits the five-hour limit. v0.1 only handled tasks that a Relay worker
   launched itself.
2. Collaborators (human and agent) could not see the coordination anywhere
   natural. Everything lived in a JSON blob on a branch.

## Decisions

| # | Decision | Why | Rejected alternative |
|---|---|---|---|
| D1 | Keep the CAS state branch as the only lease authority. Issues, comments and PRs are a mirrored conversation layer, best effort and reconciled. | GitHub issues have no compare-and-swap; two agents could both "claim" by comment. | Issues/labels as the lock. |
| D2 | Talk to GitHub through the local `gh` CLI. | No Python dependencies; GitHub auth stays in each owner's own `gh` login. | `requests` + a token in Relay config. |
| D3 | Interactive handoff is triggered by Claude Code's `StopFailure` hook with matcher `rate_limit` (verified in the hooks reference, 2026-11-06). The hook runs `relay handoff --from-hook`, which never needs a model response. | This is the only signal that fires when an interactive session is cut off by a usage limit. | Polling transcripts. |
| D4 | Handoff snapshots the owner's working tree through a temporary index; their index, HEAD and branch are untouched. Unpushed local commits travel with the checkpoint. | The owner keeps working state exactly as it was; the successor gets every recoverable byte. | `git stash` (not remote-durable), committing on the user's branch. |
| D5 | Prompts and transcripts are **not** shared by default. The successor's goal comes from `relay handoff "message"`, an earlier task the owner took, the branch's recent commit messages and `.relay-handoff.md`. `relay hook install --share-prompt` opts in to sending the last user prompt. | Transcripts are private; the v0.1 boundary said logs never leave the machine. | Sharing the prompt by default. |
| D6 | `relay take TASK` puts a task in a `held` state for a human working interactively. Held tasks never expire; workers skip them. `relay handoff` releases them. | Human priority wins over pool throughput; a person's task is not taken because their lease timer ran out. | Long expiring leases. |
| D7 | Issues labelled `relay` become tasks, but only when the issue author has write, maintain or admin permission. `/relay note`, `/relay retry`, `/relay cancel` comments are honoured only from the same roles; each comment is processed once (ids recorded under CAS). | Public repositories accept issues from anyone; repository text is untrusted input. | Label-only gate (labels can be set at triage level). |
| D8 | Each event is posted once, by the worker that caused it, with a hidden marker. A local cache avoids re-posting; a GitHub outage delays comments but never blocks checkpoints. | Single writer per event avoids duplicate comments without another lock. | One elected "mirror" worker. |
| D9 | A finished attempt opens a **draft** PR from its checkpoint branch with `Closes #N`. A merged PR marks the task `done`. Nothing merges automatically. | Review stays human; the PR is where people already review. | Auto-merge on a passing check. |
| D10 | Add worker presence (last seen every 5 min) and `--after` dependencies. Dependent tasks start from the base branch head once their dependencies are merged. | "Who is online" and simple sequencing are the two scheduler features the use case needs; anything more waits for measurements. | A cost-based scheduler. |
| D11 | Comments name people in bold, never with `@`. | Avoids notification spam from automated narration. | `@mentions`. |
| D12 | Consent is per owner and per clone: `relay start` opts in; `--accept-from` limits whose requests the agent takes; `--max-tasks-per-day` caps it; `relay add --for LOGIN` or assigning the issue addresses one collaborator's agent. Defaults: any collaborator with write access, no cap. | The owner clarified on 2026-11-06 that the core case is asking a friend's agent with spare quota to work while their machine is on, with their consent. Assignment is how GitHub already says "this is for you". | A global pool with no addressing. |
| D13 | Agent failures are classified from structured CLI output only. A top-level `error` field or HTTP 429 counts; the short error code (e.g. `authentication_failed (HTTP 401)`) is shared, message text is not. | Observed live: an expired Claude Code login emits `"error":"authentication_failed"` and `api_error_status: 401`. | Regex over the whole log. |

## Intuitions and rejected alternatives

The rejected alternative for each decision is in the table. Two broader ones:

- A hosted coordinator (web app, database, webhooks) would cut polling latency but
  add an account, hosting cost and a trust party. Git plus `gh` keeps the "no
  server" promise; revisit only if polling becomes the measured bottleneck.
- Letting agents read the whole issue thread as context would make the
  conversation richer, but issue text on public repositories is attacker-controlled.
  Only writer-authored `/relay note` text reaches prompts.

## How to verify

1. `relay demo` prints a PASS line and the issue thread: Alice's session hands off,
   Bob's agent picks it up and finishes, draft PR, merged, done.
2. `.venv/bin/python -m unittest discover -s tests -v`: 29 tests pass.
3. In a real repository with a collaborator: `relay init` in both clones,
   `relay hook install` in yours, `relay start --agent codex` in theirs, open an
   issue labelled `relay` and assign it to them. Within a minute the issue gets a
   "picked this up" comment, then a result comment and a draft PR.

## Revisit when

- A provider-observed usage limit is captured: confirm the hook payload and the
  worker classifier against it.
- Groups grow past about five workers per repository or polling cost is noticed:
  reconsider webhooks or a hosted coordinator.
- Codex gains an interactive usage-limit hook: add it alongside Claude Code's.

Spend: about $0.00 in API keys. One Codex run and one failed Claude Code start on
existing subscriptions.

## Consequences

- State stays `version: 1`; new fields are optional, so v0.1 state reads fine.
- GitHub API calls: about one list call per minute per worker for sync, plus a
  comment per meaningful event. Periodic checkpoints are not narrated.
- Codex has no equivalent interactive limit hook that we verified; for Codex and
  other CLIs the interactive path is a manual `relay handoff`.

## Independent review — 2026-11-06 (gpt-6-astra high, gemini-3.1-pro-preview)

Full text: `docs/reviews/2026-11-06-v0.2.review.md`. GPT verdict: "do not ship v0.2 as
advertised". Gemini verdict: "ship with fixes". Adjudication (reviewer finding → our call):

GPT-6 Astra
- 1.1 Retry keeps the old PR, old merge completes new work → **agree**, fixed: PRs are per attempt (`retire_pr`), `mark_done` requires the current PR. Test added.
- 1.2 Issue-creation vs import race can make a second task → **agree**, fixed: Relay-opened issues carry an ownership marker that import skips. Test added.
- 1.3 `TimeoutExpired` escapes the best-effort boundary → **agree**, fixed: converted to `RelayError` in `GitHub.api`. Test added.
- 1.4 No sync while an attempt runs, so cancel can wait an hour → **agree**, fixed: the attempt loop syncs every minute. Test added.
- 1.5 Cursor uses update time but ordering uses creation time; no pagination → **agree**, fixed: sort by `updated`, paginate. Test added.
- 1.6 Issue import limited to one page → **agree**, fixed: paginated (bounded at 20 pages).
- 1.7 Permission cache never expires; failures cached as denial → **agree**, fixed: 10-minute TTL, failures not cached (404 treated as no access). Test added.
- 1.8 "Processed once" only within the last 500 ids → **agree in part**: window raised to 5,000 and the claim narrowed in docs; an unbounded set would grow shared state forever.
- 1.9 Crash between post and cache write duplicates comments → **agree**, fixed: the thread is checked for the marker before posting. Test added.
- 1.10 Failed `take` strands a non-expiring hold → **agree**, fixed: fetch before holding, roll back to queued on failure. Test added.
- 1.11 Hook stops handing off once the branch's earlier task is done → **agree**, fixed: only an identical tree is skipped. Test added.
- 2.1 Wall-clock leases → **already documented**; README now states it in the limits list.
- 2.2 Dedupe tree is the probe, not the published snapshot → **agree**, fixed.
- 2.3 Goal may be unrecoverable without the prompt → **agree, by design** (D5): privacy default; `--share-prompt` and `relay handoff "message"` exist. Revisit with user feedback.
- 2.4 Dependencies on another branch → **agree**, documented: dependents should target the branch the dependency merges into.
- 2.5 Narration lost if the originating worker never returns → **agree**, documented as a limit.
- 3.1 Shared outbox → **disagree for now**: remote marker check removes the realistic duplicate case without another shared structure.
- 3.2 Attempt-scoped PRs → **agree**, done (1.1).
- 3.3 Render from event-time facts → **agree**, fixed: events snapshot checkpoint, check result and start SHA.
- 3.4 Silent draft downgrade → **agree in part**: fallback now matches GitHub's exact message only, and the comment says the PR is not a draft. Still unmerged, so the review boundary holds.
- 3.5 Transactional take → **agree**, done (1.10).
- 4.1 No real two-machine GitHub trace → **agree; open.** Creating a GitHub sandbox repository was blocked in this session; this is the next validation milestone and the README says so.
- 4.4 Which commit "passed" validates → **agree**, fixed: PR body and comment flag a check that changed files.
- 5.1 Package omitted store and tests → documentation of the package, not the repo; the full suite is in `tests/`.
- 5.2 OS support unstated → **agree**, README now says macOS or Linux.
- 5.5 v0.1 tasks have no `created_by` and are not mirrored → **agree**, accepted: v0.1 tasks keep working without GitHub narration.

Gemini
- Pagination → **agree**, fixed (as GPT 1.5, 1.6).
- Clock skew → **already documented**.
- Draft fallback matches any "draft" text → **agree**, fixed (as GPT 3.4).
- Hook blocks on stdin → **disagree**: Claude Code writes the payload and closes stdin; the hook only runs from that path.
- POSIX-only undocumented → **agree**, fixed.
- `relay/state` history growth → **agree**, documented as a limit; pruning is future work.
- Use urllib + token instead of `gh` → **disagree** (D2): keeps auth in the owner's own `gh` login and adds no token handling; call volume is a few per minute.
- Use issue comments with ETags as the lock → **disagree** (D1): comments have no conditional update.

Verdict after fixes: re-reviewed below. Spend: cents (about 40k tokens across both reviewers).

## Independent re-review of the fixes — 2026-11-06 (gpt-6-astra high)

Full text: `docs/reviews/2026-11-06-v0.2-fixes.review.md`. Verdict: "do not ship this
revision yet", naming two blockers. Adjudication:

- 1.1 A stale in-flight PR creation can attach to a newer attempt → **agree**, fixed: `attach_pr` links a PR only if the task is still in review at the PR's epoch; a refused PR is closed as superseded; `mark_done` also requires the PR's epoch to be the current one. Interleaving test added.
- 1.2 In-attempt sync can starve lease renewal → **agree**, fixed: all GitHub work during an attempt (sync and the post-claim comment) runs on one background thread; heartbeats never wait on GitHub. Test with a 4-second `gh` call and a 3-second lease.
- 1.3 Boundary-timestamp truncation after 2,000 comments → **agree, accepted limit**: needs more than 2,000 comments edited within one minute; documented.
- 1.4 Import beyond 2,000 open labelled issues → **agree, accepted limit**, documented.
- 1.5 Marker search on threads beyond 2,000 comments → **agree, accepted limit**, documented.
- 1.6 Orphaned issue or PR after a crash → **agree**, fixed: issues are adopted by ownership marker, PRs by head branch, before creating new ones. Test added.
- 1.7 Failed take requeues instead of restoring → **agree**, fixed: `unhold` restores the prior status, PR link and verification. Test covers a blocked task.
- 1.8 Crash between hold and local config strands the hold → **agree, documented** recovery: `relay cancel` then `relay retry`.
- 1.9 Claim from base not snapshotted → **agree**, fixed: claim events record the base SHA.
- 1.10 Checkpoints can evict unposted narration → **agree**, fixed: narrated events and checkpoints are kept in separate windows (50 and 10).
- 1.11 `detect` timeout → **agree**, fixed.
- 3.1 Superseded PRs still carry `Closes #N` → **agree**, fixed: retiring a PR closes it on GitHub with a note. Test added.
- 3.3 Non-draft fallback should be opt-in → **disagree**: the fallback PR is still unmerged and named as non-draft in the issue; making it opt-in would leave GitHub Free private repositories without a PR by default.
- 3.4 Bounded replay → **agree**, docstrings now state the 5,000-id window.
- 4.1 Two-machine GitHub trace → **open**, as before.
- 5.4 Docstrings overstate delivery → **agree**, fixed.

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

## Third independent review — 2026-11-06 (gpt-6-astra high)

Full text: `docs/reviews/2026-11-06-v0.2-fixes-2.review.md`. Verdict: "do not ship",
naming rollback and supersession invariants. Adjudication:

- 1.1 Failed take from review restores a PR that can no longer complete the task → **agree**, fixed: `unhold` re-binds the restored PR to the current epoch. Test added.
- 1.2 The rolled-back hold can still close the restored PR → **agree**, fixed: `unhold` removes the hold event's retirement; `take` posts only after success. Test added.
- 1.3 Failed take of an expired running task leaves "running" with no lease → **agree**, fixed: it returns to `queued`. Test added.
- 1.4 Closing a refused stale PR is not crash- or outage-safe → **agree**, fixed: the refusal is recorded in state (`past_prs` plus a silent `superseded` event) before closing, so a failed close is retried; the PR is closed before the explanatory comment. Test injects the outage.
- 1.5 Attachment not idempotent → **agree**, fixed: attaching the same PR at the same epoch succeeds. Test added.
- 1.6 Shutdown abandons a live GitHub job → **agree in part**: the worker now says so; recovery relies on marker and head-branch adoption, documented. A managed job queue (3.2) is future work.
- 2.1 Shared local state across threads → **agree**, fixed: state writes are serialized per clone in-process, and a push that landed but failed locally is treated as landed (never applied twice). Narration was already under a file lock.
- 2.2 PR adoption picks any PR with that head → **agree**, fixed: closed-unmerged PRs are not adopted.
- 2.3 Issue adoption bounded by the labelled listing → **agree, accepted limit**, documented.
- 2.4 Retirement depends on the acting worker returning → **agree, accepted limit** (same as narration ownership, already documented).
- 3.1 Durable retirement queue → **agree in part**: retirements are now state-recorded events retried until done; a separate queue is not added.
- 3.3 Non-draft fallback → **disagree**, as before.
- 4.2 Slow-sync test only covers failing slow calls → **agree, open**: a successful slow `gh` stand-in is future test work.
- 4.4 Real two-machine trace → **open**, as before.
- 5.2–5.4 Documentation gaps → **agree**, fixed in README, architecture and validation.

## Fourth independent review — 2026-11-06 (gpt-6-astra high)

Full text: `docs/reviews/2026-11-06-v0.2-fixes-3.review.md`. Verdict: "do not ship",
naming two blocking interleavings. Adjudication:

- 1.1 A delayed old-epoch attach can retire the PR a failed take restored → **agree**, fixed: the current PR is never recorded as superseded, and `_retire` re-reads state and never closes the current PR. Test added.
- 1.2 Push recovery replays a landed mutation if another writer pushes next → **agree**, fixed: a push counts as landed if its commit is an ancestor of the remote tip (state history is append-only: every push names its exact parent). Test injects the interleaving.
- 1.3 Retirement note lost after a partial close → **agree**, fixed: the note carries its own marker and is retried independently of the close. Test added.
- 2.1 Rollback vs another flusher's earlier snapshot → **agree**, fixed by the same current-PR guard at close time.
- 2.2 Head branch identifies the attempt → **already true**: checkpoint branches are `relay/checkpoints/<task>/<epoch>`, one per attempt; closed-unmerged PRs are skipped.
- 2.3 Retirement depends on the acting clone returning → **agree, accepted limit**; docs and the shutdown message now say "this clone" and give the manual fallback.
- 2.4 Unlocked reads → **disagree**: `read` only runs `ls-remote`, an exact-object fetch and `git show`; none takes local ref locks.
- 3.3 Repeated stale attach appends duplicates → **agree**, fixed: one supersession record per PR.
- 4.4 Successful slow-GitHub two-worker trace → **open**, with the real two-machine trace.
- 5.1, 5.3 Package lacked full context and a test record → **agree**: the next package includes the full touched functions and the suite output.

## Fifth independent review — 2026-11-06 (gpt-6-astra medium, gemini-3.1-pro-preview)

Full text: `docs/reviews/2026-11-06-v0.2-fixes-4.review.md` and `.gemini.review.md`.
GPT-6 Astra at high effort timed out three times (15-minute read timeout, then a reset);
medium effort completed. GPT verdict: "do not ship"; Gemini verdict: "ship". Adjudication:

GPT
- 1.1 `_retire` checks state before closing, so a retirement overlapping a reversible hold can close a PR the rollback restores → **agree**, fixed at the root as GPT proposes (3.1): a hold no longer unlinks or retires the PR. The PR is retired only when the holder hands back new work (a status change in a later attempt), which is irreversible. A failed take restores only the status. Overlap test added.
- 1.2 The earlier test serialized the race away → **agree**, the new test holds, retires mid-hold, then rolls back.
- 1.3 Ancestry-check errors treated as "not landed" → **agree**, fixed: exit codes other than 0 and 1 raise instead of retrying.
- 1.4 Two clones can both post the retirement note → **agree, accepted limit**, documented.
- 2.1 Append-only history assumes nobody rewrites `relay/state` → **agree**, documented as an operating requirement.
- 2.2 Absent PR link treated as permission to close → **agree**, resolved by 1.1: retirement eligibility is now monotonic.
- 3.3 Shared outbox → **disagree for now**, as before.
- 4.3 Slow-GitHub two-worker trace → **open**.
- 5.1–5.3 Docs and wrapper code missing from the package → **agree**: the next package includes them.

Gemini
- 1.1 `unhold` fails if the network is down, leaving a hold → **agree, documented** recovery (`relay cancel`, then `relay retry`).
- 1.2 `unhold` overwrote `past_prs` → **agree**, fixed: holds no longer snapshot PR fields.
- 2.1 Fetching unadvertised commits by SHA → **already required**: every read already fetches the exact state commit.
- 2.2 PR numbers as identifiers → **agree**, they are unique per repository.
- 3.2 Two fetches in `take` → **disagree**: the first prevents a hold nobody can find; the second is cheap.

## Sixth independent review — 2026-11-06 (gpt-6-astra medium, gemini-3.1-pro-preview)

Full text: `docs/reviews/2026-11-06-v0.2-fixes-5.review.md` and `.gemini.review.md`.
GPT verdict: "do not ship"; Gemini verdict: "ship with fixes". Adjudication:

GPT
- 1.1 A failed take strands an in-flight PR publication (epoch moved on) → **agree**, fixed: a hold records the held attempt's epoch; that attempt may still attach its PR during the take, and a failed take restores the epoch (safe: nothing held a lease at it and the hold published nothing). Running tasks still requeue at the new epoch. Two tests (PR lands mid-take; PR creation starts after rollback).
- 1.2 Hand-back erased its own check result → **agree**, fixed: the old PR is retired before the new verification is recorded. Test added.
- 1.3 Page-cap exhaustion silently partial → **agree**, fixed: duplicate checks (comment markers, issue adoption) raise and defer on a truncated list; command sync and import stay bounded and documented. Test added.
- 2.1 Attachment did not enforce terminal retirement → **agree**, fixed: a PR in `past_prs` is never attached again. Test added.
- 2.2 Mixed-version clones → **agree**, documented: one Relay version per repository during alpha.
- 3.1 Separate fencing from publication identity → **agree in part**: achieved by restoring the held attempt's epoch on rollback rather than a second counter.
- 3.3 Repository-visible retirement queue → **disagree for now**, as before.
- 4.2 PR merged while held → **agree**, fixed: it completes the task and clears the hold; the holder's branch is left alone. Test added.
- 4.3 Slow-GitHub two-worker trace → **open**.
- 5.4 Hold recovery runbook → **agree**, written in architecture.
- 5.1–5.3 Missing handoff, `change`, `api` code in the package → **agree**: included next time.

Gemini
- 1.1 Silent truncation → **agree**, fixed (GPT 1.3).
- 1.2 Duplicated methods in `github.py` → **disagree**: the source has none; overlapping excerpts in the package produced the duplicate. The package builder is fixed.
- 2.1 Draft fallback depends on an exact error string → **agree, accepted**: any other error surfaces as a deferred PR, not a crash.
- 2.2 String return of `attach_pr` → **already handled**: it returns a boolean.
- 3.1 `pr_epoch` rebinding hard to verify → **agree**: replaced by restoring the held attempt's epoch.
- 3.2 / 4.1 Double fetch and offline rollback → **disagree / documented** (runbook above).

## Seventh independent review — 2026-11-06 (gpt-6-astra medium, gemini-3.1-pro-preview)

Full text: `docs/reviews/2026-11-06-v0.2-fixes-6.review.md` and `.gemini.review.md`.
GPT verdict: "do not ship"; Gemini verdict: "ship". Adjudication:

GPT
- 1.1 Hand-back without an attached PR inherits the old check result → **agree**, fixed: a new checkpoint clears any stored check result unless one is recorded with it. End-to-end handoff test added.
- 1.2 Restoring the epoch reuses lease identities → **agree**, fixed as GPT proposes (3.1): epochs are monotonic again; a separate `result_epoch` names the attempt whose review result and PR are current, and survives a reversible take. Test: an old hold token matches nothing after rollback and re-take.
- 1.3 A capped issue thread blocks closing superseded PRs → **agree**, fixed: retirements run before the comment-marker scan. Test added.
- 1.4 Issue adoption misses a closed orphan → **agree in part**, fixed for closed issues (search all states); an orphan whose label was removed by hand is an accepted limit.
- 2.1 / 5.1 Concurrent interactive commands in one clone → **agree**, fixed: `take`, `handoff` and the hook share a per-clone lock.
- 3.3 Retirement independent of comment dedupe → **agree**, done (1.3); a shared queue stays out of scope.
- 4.4 Delayed-GitHub two-worker trace → **open**.
- 5.4 Pagination exceptions contained → **already true**: `flush` catches per task and `flush_all` calls it per task; import and command sync never raise on the cap.

Gemini
- 1.1 One capped thread starves other tasks → **already contained** (see 5.4 above).
- 1.2 Handing back after the PR merged mid-hold creates a new task → **agree, by design**: the work is new; the hand-off now says so.
- 3.1 `relay unhold --force` → **disagree for now**: cancel then retry is explicit and documented.
- 3.2 Double fetch → **disagree**, as before.

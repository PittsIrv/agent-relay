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

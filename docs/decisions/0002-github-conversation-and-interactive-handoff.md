# 0002: GitHub as the conversation, interactive handoff, small scheduler

Date: 2026-11-06. Status: accepted (owner delegated all v0.2 decisions).

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

## Consequences

- State stays `version: 1`; new fields are optional, so v0.1 state reads fine.
- GitHub API calls: about one list call per minute per worker for sync, plus a
  comment per meaningful event. Periodic checkpoints are not narrated.
- Codex has no equivalent interactive limit hook that we verified; for Codex and
  other CLIs the interactive path is a manual `relay handoff`.

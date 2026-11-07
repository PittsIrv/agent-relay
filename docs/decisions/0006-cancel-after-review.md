# 0006: Cancel after review closes the PR

Date: 2026-11-07. Status: accepted. Who decided: Router (as manager), after the concurrency
test showed that cancelling a reviewed task left its PR open.

## Rule

- A cancel that lands after a task reached review closes that attempt's PR. The PR gets a
  comment first ("Closed by relay cancel from <owner identity>; the branch is kept.") and is
  then closed. Nothing is deleted: the branch stays and the task keeps its PR link, so a
  person who reopens the PR undoes the close. A reopened PR does not complete the cancelled task.
- Only the task's owner identity may cancel a task that has reached review: the identity that
  requested it (`requested_by`), or, when none is recorded, the clone that created it. Anyone
  else is refused: the CLI errors with "Only <owner> can cancel a task that has reached
  review"; a `/relay cancel` comment gets a reply on the thread saying it was not applied.
- Cancelling a task that is queued, running, blocked or held is unchanged and needs no ownership.

## Why

A PR from a cancelled task still carries `Closes #N`. Left open it invites a merge of work the
owner has withdrawn, and the review queue keeps a dead item. Restricting review-stage cancels to
the owner stops a collaborator from discarding a reviewer's pending work.

## Revisit when

An owner wants a reviewed PR kept after cancel: add `--keep-pr`.

## How it works

`cancel` records the PR number on the `cancelled` event (`cancel_pr`). The clone that cancelled
narrates it: it comments on the PR (once, by marker) and closes it if still open and not merged.
A failure is deferred and retried on the next flush.

## Verify

`python -m unittest tests.test_github -k reviewed -k owner`; the e2e cancel scenario covers it
over real GitHub.

## Independent review — 2026-11-07 (gemini-3.1-pro-preview, middle tier; also covers PR #3)
- Refusal reply lost on a network error, command still marked handled → disagree — the reply is a courtesy, the command must not re-run, and `sync` still reports `refused`; GitHub calls here are best-effort by design.
- Review without a recorded PR bypassed the ownership check → agree — the rule now applies to every task in review; the PR is closed only if there is one.
- `requested_by` assumed to be a string → disagree — only ever written from a login or owner name.
- Owner-only has no maintainer override → noted for the Router; kept as ruled. Revisit with `--keep-pr`: if an owner leaves, add a maintainer override.
Verdict: ship with fixes. Action taken: first fix above.

# 0005: End-to-end scenarios over real GitHub

Date: 2026-11-07. Status: accepted. Who decided: Worker (small decision).

## Intent

The project's number is how many handoff scenarios pass over real GitHub between two
separate clones. The offline suite proves the logic against a stand-in; this proves the
real `gh` calls, issue/PR behaviour and Git refs agree with it.

## Decisions

- `scripts/e2e_github.py`, not a unit test: it does nothing unless `RELAY_E2E_REPO` is
  set, so the default suite and CI never call GitHub. It refuses a public repository.
- Two clones of a private scratch repo with owners `alice` and `bob`, both acting through
  the one signed-in `gh` login. **Limit:** scenarios that need two GitHub logins (write
  access revoked, an issue assigned to another login) stay in the fake-gh suite.
- The CLI (`relay add/take/handoff/retry/sync`) is driven as a subprocess; workers run
  in-process with the offline mock agents; no model calls. The usage-limit hook is driven
  with a synthetic `StopFailure` payload; the real hook test needs a signed-in Claude Code.
- Rerunnable: the script closes the Relay-opened issues (ownership marker) and PRs (`relay/*`
  heads) and deletes `relay/*` branches in the scratch repo at the start and end, and seeds `main` if the repo is empty.

## Scenarios and results (45/45)

take 7/7, hand back 9/9, supersede 5/5, cancel 16/16, usage-limit handoff 8/8. The cancel
scenario includes the Router's rule (decision 0006): cancel after review is owner-only,
closes the PR with a note, keeps the branch, and reopening the PR undoes it. First run,
before that rule: 36/36.

## Script bugs found on the way (not product bugs)

Alice's tree stayed dirty after a handoff, so a later `take` was correctly refused and the
stray queued task was claimed in the cancel scenario. The script now cleans between steps.

## How to verify

`RELAY_E2E_REPO=you/scratch python scripts/e2e_github.py`

## Independent review — 2026-11-07 (gemini-3.1-pro-preview, middle tier)
- Cleanup closes every open issue/PR, unsafe if pointed at the wrong private repo → agree (verdict "do not ship") — cleanup now touches only Relay-owned items, is paginated, and the repo name must contain sandbox/scratch/e2e/test; docs say so.
- No pagination in cleanup → agree — fixed.
- Assumes exclusive access / token scopes → agree, documented in the header.
- Polling the store from the main thread risks a JSON error → disagree — the store is read from Git refs, which update atomically.
- Rewrite as unittest with skipUnless → disagree — the per-scenario pass counts are the project's reported number; a script prints them directly.
- gh failure leaves orphans → disagree — temp dir is a context manager and cleanup runs in `finally`.
Verdict: do not ship. Action taken: safety fixes above, re-run below.

## Second pass — 2026-11-07 (gemini-3.1-pro-preview)
Verdict: ship with fixes. A failed setup or a not-yet-fetched task crashed the whole run
instead of failing its scenario → agree — each scenario now runs guarded and lookups are
tolerant. `--json` CLI output and a hard-abort `check()` → not now (no CLI surface change;
soft checks keep the per-scenario counts). Re-run after the change: 36/36.

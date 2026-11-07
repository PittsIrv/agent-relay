# 0007: A stranger's first handoff, tested from the README alone

Date: 2026-11-07. Status: accepted. Who decided: Router (ticket T-0036); implementation is the Worker's.

## Intent

The number: a scripted fresh-clone run where a new user follows only the README and completes
take → hand back on a private scratch repo, from a clean temp directory, in 10 minutes or less.

## Decisions

- `scripts/first_run_test.py`, opt-in via `RELAY_E2E_REPO` like `e2e_github.py` (same
  private/scratch-name guard, same cleanup of what Relay opened). Never run by CI.
- It does not know the commands: it reads them from the README (code blocks and inline
  `relay …` spans) and runs them literally, in a temp directory, with the tool installed into
  the temp directory. A command the path needs that the README lacks is a failed run.
- Minutes are the commands' wall-clock from install to the handed-back task; reading time is
  not counted. `RELAY_FIRST_RUN_SOURCE` installs from a checkout instead of the published git
  URL, so a README or CLI fix can be checked before it merges.
- Limits: one GitHub login; no model calls; the real hook is not run.

## Gaps found, each fixed in its own PR

(listed below as they are found)

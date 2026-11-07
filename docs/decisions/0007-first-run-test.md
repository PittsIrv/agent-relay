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

## Independent review — 2026-11-07 (gemini-3.1-pro-preview, middle tier)
- Comment stripping by regex breaks quoted `#` → agree — removed; the shell ignores trailing comments.
- A single step can eat the whole 10-minute budget → agree — per-step timeout is 3 minutes.
- Prefix matching picks the first command; tags in the README would be safer → disagree — the README stays plain prose; the first match is what a reader meets first, and the test fails if it is wrong.
- Placeholder, status format, GitHub-URL install assumed → acknowledged; they fail loudly when the README changes.
Verdict: ship with fixes. Action taken: the first two.

## Gaps found, each fixed in its own PR

(listed below as they are found)

- Script: commands are read in document order (code blocks and inline `relay …` spans interleaved). An early version scanned code blocks first and ran `handoff --review` where the README's first mention is the plain hand back.
- Review of this change (Gemini, middle tier): ship with fixes; regex parsing of Markdown is fragile (inline spans in prose, multi-line commands). Acknowledged, not changed: a wrong pick fails the run loudly, and the README's commands are single-line by convention.

- Gap 2 (errors with no next step), found by running the mistakes a first-timer makes: outside a Git
  repository, no `origin` remote, `add` on a branch that is not pushed, an unknown task id (take, show,
  cancel, retry, note), and `init` finding no usable `gh` (it silently turned GitHub off). Each message
  now names the command to run next. One PR for all of them: they are the same change (message text) and
  share one test file, `tests/test_first_run_errors.py`, which drives the CLI the way a new user does.
- Review of the error messages (Gemini, middle tier): ship with fixes. Blanket rewrap of every `rev-parse` failure → agree, now only when git says "not a git repository". The `gh auth login` hint was unconditional → agree, reworded to apply only if the repository is on github.com. Move the advice into the CLI layer with typed exceptions → disagree: one entry point, one tool; the strings live next to the raises, which keeps them from drifting.

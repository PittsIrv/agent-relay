# 0009: `relay doctor`

Date: 2026-11-07. Status: accepted. Who decided: Router (ticket T-0056); details are the Worker's.

## Intent

A stranger's first five minutes decide whether they stay. `relay doctor` answers "will my setup work?"
before they try, with a fix for every problem.

## Decisions

- Read-only, one line per check: `ok`, `warn` (optional or advisory) or `FAIL` (blocks a first handoff),
  each non-ok line followed by `fix: <command or step>`. Exit code 1 only on `FAIL`; `--json` prints
  `{ok, checks:[{name,status,detail,fix}]}`.
- Checks: platform (POSIX, with a WSL2 message for Windows), Python 3.11+, Git 2.31+ (the oldest with
  `--path-format=absolute`), Git identity, inside a repository, `relay init` done, the remote exists and is
  readable, `gh` installed and signed in (only when the remote is on github.com), GitHub mirroring on, push
  access (GitHub permission when `gh` works, otherwise `git push --dry-run`), `relay/state` present and a
  supported format (absent is fine: the first `relay add` creates it), Claude or Codex on PATH (warn),
  hook installed (warn), clock against GitHub's `Date` header (warn over 30 s, fail over 120 s, because
  leases use the wall clock; warn when GitHub cannot be reached), a dirty working tree (warn: `take`
  refuses it).
- It runs without a repository or enrollment (the point of the command): it reports the first missing
  prerequisite and stops where later checks make no sense.
- Never prints secrets. It uses exit codes and version strings only, discards `gh auth status` output, shows
  the repository as `owner/name` instead of the remote URL, and redacts token patterns, `GH_TOKEN`-style
  values and URL credentials from everything it prints. Tested with a `gh` that echoes a token and a remote
  URL containing a password.
- Tests use the offline fake `gh`; a remote named `https://github.com/demo/shared.git` is mapped to the
  local bare repo with Git's `insteadOf`, so the GitHub branches run without a network.
- Not checked: whether the agent CLIs are signed in (no safe, provider-neutral probe), and real provider
  limits.

## Independent review — 2026-11-07 (gemini-3.1-pro-preview, middle tier)
- Push probe fails misleadingly in a clone with no commits → agree — says to fetch or commit first.
- Redaction too aggressive (short `GH_TOKEN`; `ssh://git@`) → agree — env values under 8 characters are ignored; only `http(s)://user[:pass]@` is stripped.
- Assumes `origin` before init → agree — uses the only remote when there is exactly one.
- Fix text hard-codes the Python test command → agree — now `relay init` with a placeholder for `--check`.
- GitHub as clock source for non-GitHub hosts → disagree — the clock check only runs when the remote is on github.com.
- Agents found only on PATH → acknowledged; it is a warning, and `relay start` needs them on PATH anyway.
- `push --dry-run` side effects → disagree — a dry run only advertises refs; it runs no server-side hooks.
- 30 s git timeout → left as is; a slower remote is itself worth knowing about.
Verdict: ship with fixes. Action taken: the first four.

## Demo in the README (T-0056 part 2)

- No recorder is installed and none is needed: `scripts/make_demo_svg.py` runs the real offline `relay demo`
  and writes `docs/assets/relay-demo.svg`, an animated terminal (CSS keyframes, one line at a time, looping,
  all lines at once under reduced motion; no script, so it plays inside GitHub's image viewer) and
  `docs/demo-transcript.txt`. The README embeds the SVG and, in a `<details>` right under it, the same text
  for screen readers and copy/paste; the SVG has a `<title>` and `<desc>`.
- Scrubbing: the demo output holds only "alice" and "bob" and short hashes. The script refuses to write
  if the output contains a local path or an email address; `tests/test_demo_assets.py` checks the SVG, the
  transcript and the README for the same, that the SVG is valid and animated, and that the README transcript
  is the file's text and every line of it is in the SVG.
- The README's setup section now has `relay doctor` right after `relay init`; `scripts/first_run_test.py`
  runs it from the README and requires exit 0 (a clean clone of the sandbox, 12 steps, 0.5 minutes of commands).
- Not done: a real-terminal recording (asciinema or similar). A hand-rolled SVG of real output is reproducible
  and needs no tool; revisit if the demo should show real agents.

## Independent review of the demo — 2026-11-07 (gemini-3.1-pro-preview, middle tier)
- The generator replaced `PATH` with a Unix list → agree — it inherits the environment now.
- The test's scrubbing patterns differed from the generator's → agree — the test imports the generator's list.
- The test skipped lines containing backticks → agree — no reason to; removed.
- The README transcript is copied by hand → agree — the generator rewrites the block between `demo-transcript` markers; the test says to run it when stale.
- A wider fallback monospace font could clip a line → acknowledged: the box is sized with margin and the full text sits right under the image.
Verdict: ship with fixes. Action taken: the first four.

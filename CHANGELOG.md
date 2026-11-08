# Changelog

## Unreleased

- The README opens with an animated terminal demo of the offline handoff, with its full text beside it, and
  its setup section points to `relay doctor`. The demo is regenerated from a real `relay demo` run by
  `scripts/make_demo_svg.py`.
- `relay doctor` checks what a first handoff needs, one line per check with a fix for each problem:
  platform, Python, Git version and identity, repository, enrollment, remote access, `gh` installed
  and signed in, push access, `relay/state` readable, agent CLIs, the usage-limit hook, clock skew
  against GitHub and a dirty working tree. Exit code 1 on any failure; `--json` for scripts. It never
  prints a token or a credentialed URL.

## 0.3.0 (alpha)

Proven over real GitHub, and easier for a stranger to start.

- **End-to-end scenarios over real GitHub.** `scripts/e2e_github.py` drives take, hand back,
  supersede, cancel and the usage-limit handoff between two clones against a private scratch
  repository (45 checks). Opt-in through `RELAY_E2E_REPO`; the default suite never calls GitHub.
- **Cancel after review closes the PR.** A cancel that lands once a task has a PR in review
  comments on the PR and closes it (branch kept; reopening undoes it). Only the person who
  requested the task can do this.
- **Concurrency.** Two workers against a slow GitHub: each task is claimed once, narrated once
  and gets one PR; two clones importing the same issues create one task each.
- **First run.** The README works from a clean machine: an install path without `pipx`, a clone
  step, and errors that say what to do next (not in a Git repo, no remote, branch not pushed,
  unknown task id, GitHub off). `scripts/first_run_test.py` follows the README literally and
  completes take → hand back in under a minute of commands.
- Test fixes: the fake GitHub state is edited under its lock, which removed a CI flake.

## 0.2.0

Hand work to a collaborator's agent through GitHub issues and pull requests; interactive
`take` / `handoff` and a Claude Code usage-limit hook.

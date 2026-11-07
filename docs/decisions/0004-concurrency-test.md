# 0004: Two-worker concurrency test against injected GitHub latency

Date: 2026-11-06. Status: accepted. Who decided: Worker (small decision).

## Intent

Show that two clones acting at the same moment against a slow GitHub keep the
protocol's guarantees: a task is claimed once, narrated once, and gets one PR.

## Decisions

- The fake `gh` takes `FAKE_GH_LATENCY` (seconds, upper bound of a uniform random
  delay spent before the state lock, like a real round trip). `demo.setup(latency=...)`
  passes it to both clones. Default 0, so existing tests are unchanged.
- `tests/test_concurrency.py`, three scenarios at up to 0.8 s per GitHub call:
  1. Two workers drain four queued tasks: each task has exactly one claim, one issue,
     one open PR on its own branch, and no duplicate comment.
  2. Both clones import the same three labelled issues at the same instant (barrier):
     one task per issue, one acknowledgement per issue.
  3. A `/relay cancel` is posted the moment the task is claimed while the mock agent
     (`finish-fresh`: writes `answer.py` and exits at once) is finishing: the outcome is either
     cancelled with no open PR, or review with exactly one open PR; never both.
- Rejected: sleeps tuned to provoke a particular interleaving. Scenario 3 asserts the
  invariant for either ordering instead.
- Not seeded: each `gh` call is a new process and thread scheduling is not
  reproducible, so a seed would not make a failure replayable; the assertions are
  invariants that hold for any ordering.

## Result

All three pass repeatedly (scenario 1 ~31 s, scenario 2 ~7 s, scenario 3 ~10-13 s).
No product defect was found; no product code changed.

## How to verify

`python -m unittest tests.test_concurrency`

## Independent review — 2026-11-06 (gemini-3.1-pro-preview, middle tier)
- watcher thread without try/finally → agree — stop flag now set in `finally`.
- unseeded latency → disagree — a seed cannot make a multi-process, multi-thread run replayable.
- hardcoded 2.5 s sleep aligned to the mock → agree — removed; the cancel is posted at claim time against an instantly finishing agent.
- fcntl on Windows → disagree — the worker itself locks with fcntl; POSIX only.
- tuned sleeps contradict the record → agree (same fix).
- mock behaviour undocumented → agree — described above.
Verdict: ship with fixes. Action taken: the three fixes above. Cost: about two cents of Gemini.

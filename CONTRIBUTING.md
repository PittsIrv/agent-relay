# Contributing

Keep the two-person setup short: Python standard library, Git and `gh`. Prefer standard-library code and existing Git/CLI
interfaces over required infrastructure. Changes should preserve local ownership,
isolated attempts, recoverable work and explicit review before integration.

To run the tests from a checkout on macOS or Linux (about nine minutes; they use real Git and
child processes):

```sh
python3 -m venv .venv && .venv/bin/pip install -e . && .venv/bin/python -m unittest discover -s tests -v
```

The opt-in GitHub scenarios (`scripts/e2e_github.py`, `scripts/first_run_test.py`) need
`RELAY_E2E_REPO=owner/scratch-repo`, a private repository you own whose name contains
sandbox, scratch, e2e or test; see the README's Development section.

Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` and
`PYTHONPATH=src python3 -m agent_relay demo` from a checkout. Add failure-oriented
tests for coordination changes. Do not call paid models or GitHub in the default test suite;
use the mock agent and `agent_relay/fake_gh.py`. `scripts/e2e_github.py` is the opt-in
exception: it needs `RELAY_E2E_REPO` set to a private scratch repository. Record design choices in
`docs/decisions/`.

Open an issue with a reproducible scenario before adding a dashboard, scheduler
framework or new service. Provider adapters should report unknown telemetry as
unknown. Include sanitized fixtures for new error formats, without user logs or
authentication data. Describe measured behavior separately from proposed features.

Contributions are licensed under Apache-2.0. By submitting a contribution you
confirm you have the right to contribute it under that license.

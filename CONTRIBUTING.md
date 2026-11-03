# Contributing

Keep the two-person setup short. Prefer standard-library code and existing Git/CLI
interfaces over required infrastructure. Changes should preserve local ownership,
isolated attempts, recoverable work and explicit review before integration.

Run `PYTHONPATH=src python3 -m unittest discover -s tests -v` and
`PYTHONPATH=src python3 -m agent_relay demo` from a checkout. Add failure-oriented
tests for coordination changes. Do not call paid models in the default test suite.

Open an issue with a reproducible scenario before adding a dashboard, scheduler
framework or new service. Provider adapters should report unknown telemetry as
unknown. Include sanitized fixtures for new error formats, without user logs or
authentication data. Describe measured behavior separately from proposed features.

Contributions are licensed under Apache-2.0. By submitting a contribution you
confirm you have the right to contribute it under that license.

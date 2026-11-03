# Validation scope

The test suite uses temporary bare Git remotes, independent clones and real child
processes. It never spends provider quota or writes to GitHub.

Covered scenarios:

- Interrupted fake agent leaves uncommitted edits; another clone fetches and completes them.
- Competing claims yield one lease owner.
- Expired workers cannot overwrite a successor's state.
- Pausing preserves partial work and releases the task.
- Paused workers do not claim tasks.
- Sensitive checkpoint filenames block publication and retain local work.
- Authentication-style agent failure blocks instead of causing retry churn.
- Snapshot creation leaves the checkout's index and HEAD unchanged.
- Ordinary agent text mentioning rate limits is not mistaken for a limit event.
- Failed checkpoint pushes retain local recovery coordinates without advertising them.
- Common generated caches stay out of snapshots; retries preserve checkpoint pointers.

`relay demo` is the reproducible end-to-end demonstration. It also verifies the
original main branch remains unchanged and the receiving worker's check passes.

Provider command flags were checked against locally installed `codex exec --help`
and `claude --help` during initial development. This checks CLI shape only. No real
provider session, subscription permission, exact quota reset, cross-device timing,
or universal secret detection is certified by this release.

Before calling a provider adapter validated, record CLI version, platform, auth
mode (never credentials), sanitized structured error type, remote checkpoint SHA,
successor start, verification result, and owner pause behavior. Keep private
transcripts outside public bug reports. Explicitly distinguish simulated limits
from provider-observed limits.

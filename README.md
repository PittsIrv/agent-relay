# Agent Relay

**Pass unfinished coding tasks between collaborators' local agents.**

Your agent stops. An available collaborator's agent picks up the latest checkpoint.
Each person keeps their own machine, CLI login, and control over when they contribute.

**Alpha:** the offline two-clone workflow is tested. Claude Code and Codex adapters
are experimental; live provider quota exhaustion and multi-device operation have
not yet been validated. This project does not promise additional subscription capacity.

## Small by design

- **No runtime Python dependencies.** Python 3.11+, Git, macOS or Linux.
- **No hosted coordinator, database, API key, or Relay account required.** A shared
  Git remote holds task state and checkpoint branches.
- **Use your existing coding CLI.** Sign in through Claude Code or Codex itself.
- **One foreground worker per clone.** Stop it or pause it whenever you want.
- **No automatic merges.** Finished attempts are marked for human review.

## Try it without a model

Install with [pipx](https://pipx.pypa.io/stable/installation/) or `uv tool install`:

```sh
pipx install 'git+https://github.com/PittsIrv/agent-relay.git'
relay demo
```

The demo creates a temporary bare Git remote and two independent clones. A fake
agent leaves unfinished code and reports a usage limit. The second worker fetches
that checkpoint, finishes it, and passes a check. Everything is local; the demo
does not call a model or access your GitHub account.

For a source checkout:

```sh
PYTHONPATH=src python3 -m agent_relay demo
```

## Use with a collaborator

Both people need a clone of the **same trusted repository**, Git push access, a
configured Git author, and an installed, authenticated coding CLI. Keep clocks
synchronized. The repository needs at least one pushed commit.

In each person's clone:

```sh
relay init
```

One person publishes a task, then each available person starts a worker:

```sh
relay add 'Fix the pagination bug and add a regression test'
relay start --agent codex
# Or: relay start --agent claude
```

`start` is explicit consent to pick up queued tasks in this repository. It stays
in the foreground; leave the terminal open. It does not change the CLI's login
or enable permission-bypass flags. If a task needs tools your CLI policy denies,
it can fail or produce incomplete work. Review and configure permissions locally.

In another terminal:

```sh
relay status
relay pause              # reserve this clone's worker; checkpoint and release its task
relay resume             # let an already-running worker accept tasks again
relay show TASK_ID       # inspect the handoff history and checkpoint branch
relay retry TASK_ID      # requeue a blocked or review task after inspecting it
```

Ctrl-C gracefully checkpoints and releases current work. `start --once` attempts
at most one task. `pause` controls only this clone, not all sessions using the same
subscription. Interactive sessions are **not automatically detected** in this alpha.

Configure a local verification command if your project has one:

```sh
relay init --check 'python -m unittest discover -s tests'
```

Commands are parsed into arguments and run without a shell. Shell operators such
as `&&` are not supported; use a checked-in script if needed. Each collaborator
chooses their own check locally. A passing check is recorded with its input commit,
but is not a substitute for review. Test failures still enter `review` with the
failed result visible. No configured check means **unverified**, not success.

## What happens on interruption?

1. A worker atomically claims a task on `relay/state`.
2. It creates an isolated, detached worktree from the task's remote base or checkpoint.
3. Its supervisor periodically pauses the process group and snapshots files without
   requesting an LLM response. Snapshots may contain incomplete application writes.
4. It publishes an unvalidated checkpoint to an attempt-specific branch, then
   records that exact SHA in shared state.
5. A recognized structured usage-limit error requeues the task and puts that local
   worker into a conservative cooldown. Another enrolled worker can claim it.
6. A normally completed attempt goes to `review`; the base branch stays unchanged.

Default checkpoint interval: 120 seconds. Lease: 300 seconds; heartbeat: 60 seconds.
Default cooldown: 30 minutes; maximum agent session: one hour. A cooldown is a
backoff policy, **not a claim about the provider's reset time**. Unknown errors
block the task for inspection instead of repeatedly launching agents.

```sh
relay start --agent codex --checkpoint-seconds 120 --cooldown-seconds 1800
```

Abrupt host loss can lose edits since the last **successfully published** checkpoint.
Git outages stop the attempt; local work and recovery coordinates remain under
`.git/agent-relay/attempts/`. A successor only receives the last advertised remote
checkpoint. There is no transparent migration of private model reasoning or sessions.

## Privacy and trust

Task prompts, code snapshots, optional `.relay-handoff.md`, Git author metadata and
coordination events are shared through the remote. They are public if the remote
is public. Provider logs, local verification commands and authentication stay local.

An initial checkpoint guard blocks common credential filenames/patterns, symlinks,
submodules and oversized snapshots. **It is a heuristic, not a guarantee that code
contains no secrets.** Use a repository suitable for sharing, Git ignores, and your
normal secret scanning. Repositories containing `.env.example` also hit the
conservative filename guard in this alpha. Blocked files remain local for inspection.
Common generated directories (`__pycache__`, `.venv`, `node_modules`, `.pytest_cache`)
are excluded from staging; add project-specific generated files to `.gitignore`.

This is for trusted collaborators. Git write access is the coordination trust
boundary; any collaborator can alter queue state. Lease checks fence cooperative
supervisors' metadata updates, not arbitrary commands an agent can execute. Use
your provider's sandbox and a dedicated development environment as appropriate.

Provider subscriptions retain their own terms and limits. Relay does not collect
tokens, proxy model requests or bypass limits. Cross-owner consumer-plan automation
needs provider-specific validation; local authentication alone does not settle it.

## Development

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m unittest discover -s tests -v
```

The tests use real Git remotes and subprocesses, but fake coding agents. See
[architecture](docs/architecture.md), [validation](docs/validation.md), and
[contributing](CONTRIBUTING.md). Apache-2.0 licensed.

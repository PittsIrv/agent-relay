# 0003: Test helper takes the fake gh lock

Date: 2026-11-06. Status: accepted. Who decided: Worker (small decision).

## Context

`test_cancel_comment_stops_a_running_agent` failed intermittently on CI (the
same commit passed on the pull-request run and failed on the push run, on macOS
and one Linux job) and reproduced locally about once in twelve runs under load.

## Cause

The test appended the `/relay cancel` comment with an unlocked read-modify-write
of `github.json`, while the running worker was narrating the claim through the
fake `gh`, which holds an exclusive file lock. When the two overlapped, the
worker's write replaced the test's, the cancel never arrived, and the mock
agent ran to its 60 s end. No product code was involved.

## Decision

The test helper now reads and writes `github.json` under the same `flock` as the
fake `gh`. No sleeps, no skips. Product code is unchanged.

# 0008: Getting the alpha ready to publish (T-0039)

Date: 2026-11-07. Status: accepted; v0.3.0 published. Who decided: Router (ticket T-0039); steps are the Worker's.

Goal: every checklist item a Worker can do is met; publishing (tag, GitHub release, package,
history rewrite, public post) is left to the owner with exact commands at the end.

## Log

- README clone step: the README now gives `gh repo clone OWNER/REPO && cd REPO`, and the
  first-run test runs that command (OWNER/REPO filled in) instead of cloning on its own.
- Review (Gemini, middle tier): ship with fixes; suggests hardcoding the clone in the test instead of scraping the README. Disagree: the point of the test is that the README's own command works; a README edit that breaks the scrape fails the run loudly, which is the intended signal.
- Release notes: `docs/release-notes-v0.3.0.md` is the text for the GitHub release (used by
  `gh release create --notes-file`). A rendered page for the owner was published separately.
  No tag or release was created.
- Review of the release notes (Gemini, middle tier): ship with fixes. Added what is shared (checkpoint branch + issue text visible to repository collaborators; heuristic secret guard), prerequisites, and marked the hook experimental. Not done: disabling the hook (it is opt-in via `relay hook install`).
- Version bump to 0.3.0 (string, pyproject), CHANGELOG, README status line and a Known limits section
  (one shared login in e2e; real usage-limit hook untested; provider/multi-device unobserved;
  owner-only cancel), minimal how-to-run-tests in CONTRIBUTING, a What's-new entry.
- Hygiene scan (41 commits, all refs, 2026-11-07): no secrets (key/token patterns, private keys), no
  local paths or personal data in any file content, no sensitive filenames. One finding, commit
  metadata only: the author field of 64 author/committer lines carries a university email address, and
  9 carry a real name with a GitHub noreply address. Not rewritten (history rewrite is the owner's call).
  Future commits can use the noreply address: `git config user.email <id>+<login>@users.noreply.github.com`.
  LICENSE (Apache-2.0) is present; CONTRIBUTING has how to run the tests.
- Review of the v0.3 bump (Gemini, middle tier): ship with fixes. Test command chained on one line → agree. macOS/Linux stated in CONTRIBUTING → agree. E2E variable pointed to from CONTRIBUTING → agree. Rewrite history before publishing → left to the owner as instructed, and noted that the repository is already public, so previously fetched copies keep the old metadata either way. Run the real hook before claiming it works → the README already says it is untested; it needs the owner's login.

## Published — 2026-11-07

Tag `v0.3.0` (annotated, at `f505091`) and the GitHub pre-release "Agent Relay 0.3.0 (alpha)" exist
(https://github.com/PittsIrv/agent-relay/releases/tag/v0.3.0), created by the owner, not by the
Worker. The Router relayed the owner's approval ("Publish") for the tag and pre-release only; it did
not cover PyPI, a history rewrite, or any post. Checked afterwards: the release is a non-draft
pre-release with the notes from `docs/release-notes-v0.3.0.md`; `pip install` from the tag in a clean
temp directory gives `relay --version` = 0.3.0 and `relay demo` passes. Main was `f505091` with CI green
at the time of tagging.

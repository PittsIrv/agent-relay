# 0008: Getting the alpha ready to publish (T-0039)

Date: 2026-11-07. Status: in progress. Who decided: Router (ticket T-0039); steps are the Worker's.

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

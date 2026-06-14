# Per-repo VPS skill (example)

Rename this file to `<owner>__<repo>.md` (e.g. `LMAFR__sunward.md`) and the poller
will tell Claude Code to read it before working on issues for that repo.

Put repo-specific guidance here, for example:
- Where the relevant code lives and the project's conventions.
- How to run the tests / build before opening the PR.
- Branch naming, PR template, reviewers to tag.
- Anything Claude should NOT touch.

This is optional — without a matching file, Claude uses the generic dispatch prompt.

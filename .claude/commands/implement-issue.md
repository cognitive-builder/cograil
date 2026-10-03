Implement GitHub issue #$ARGUMENTS in this repository.

1. Read CLAUDE.md, then the issue. Read only the documents the issue links to.
2. Check the issue's lane label against this session's model and effort (CLAUDE.md, Lanes). If they differ, say so in one line and continue.
3. Create the branch `claude/issue-$ARGUMENTS-<short-slug>` from `main`.
4. Ask the scout helper for the files you need instead of reading widely.
5. Make the change, with tests in proportion to it (CLAUDE.md, Testing Budget).
6. Ask the checker helper to run `scripts/check.sh quick` while working, and `scripts/check.sh full` once before the pull request (`quick` only, for docs or config changes).
7. Ask the reviewer helper to review the diff, and fix everything it marks MUST FIX.
8. Open the pull request titled `<type>: <summary> (#$ARGUMENTS)` using the template, ticking each acceptance criterion with evidence.
9. Stop. Do not start other work in this session.

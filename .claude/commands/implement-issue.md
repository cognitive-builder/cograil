Implement GitHub issue #$ARGUMENTS in this repository.

1. Read CLAUDE.md, then the issue. Read only the documents the issue links to.
2. Check the issue's lane label (CLAUDE.md, Lanes and Helpers). If this session is not running the lane's model, stop now: reply with the model and effort the lane requires so the session can be restarted with them. For a `lane:3` issue, reply that it belongs to `@claude` on GitHub. Do not continue in the wrong lane.
3. Create the branch `claude/issue-$ARGUMENTS-<short-slug>` from `main`.
4. Ask the scout helper for the files you need instead of reading widely.
5. Make the change, with tests in proportion to it (CLAUDE.md, Testing Budget).
6. Ask the checker helper to run `scripts/check.sh quick` while working. Before the pull request, run the check the Testing Budget table names for this change: `quick` for docs, config or a small fix; `full` for a feature or a safety-core change; `e2e` once, only if the issue is labelled `needs:e2e`.
7. Commit everything. The working tree must be clean before review.
8. Ask the reviewer helper to review the branch, and fix everything it marks MUST FIX (commit again after fixing).
9. Open the pull request titled `<type>: <summary> (#$ARGUMENTS)` using the template, ticking each acceptance criterion with evidence.
10. Stop. Do not start other work in this session.

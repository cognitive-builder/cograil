Implement GitHub issue #$ARGUMENTS in this repository.

Before writing code:
1. Read CLAUDE.md in full.
2. Read the issue, its acceptance criteria, its Epic, and any ADR or Product Plan section it links.
3. Create the branch `claude/issue-$ARGUMENTS-<short-slug>` from `main`.

While working:
- Stay inside the issue's scope. If you find a second problem, open a new issue and leave it.
- Follow the Vocabulary and Coding standards sections of CLAUDE.md exactly.
- Add or update tests first for GateRequired and ToolNotAllowed paths when the change touches the runner, gates or registry.
- Run `uv run ruff format . && uv run ruff check . && uv run mypy src/ && uv run pytest -q` before opening the PR.

When done:
- Open a pull request titled `<type>: <summary> (#$ARGUMENTS)` using .github/pull_request_template.md.
- Tick each acceptance criterion with evidence (command and output summary).
- List any dependency added with its licence, and any open questions.
- Do not edit CLAUDE.md, docs/Product Plan.md or ADRs unless the issue asks for it.

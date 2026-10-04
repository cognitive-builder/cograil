# ADR 0015: Autopilot

Status: Proposed · Date: 2026-10-04

## Context

The goal is to build the backlog task by task without the owner starting sessions or merging. People step in only for tasks labelled `needs:human`. CodeRabbit is off for this repo until the launch phase.

## Decision

A dispatcher, `scripts/autopilot/dispatch.py`, runs on the owner's Mac under launchd every 30 minutes while the owner is logged in. It uses the Python standard library, talks to GitHub through `gh`, and starts sessions through `claude`. It runs on the Mac because a cloud session paid by the cloud-session credit needs the owner's Claude login. That login must not be copied into GitHub Actions secrets. Starting sessions from GitHub Actions is out of scope.

It runs one task at a time, all lanes included. Tasks build on each other, so parallel sessions would conflict on the same files and double spend before anyone notices. One line per task on the ledger is easy to audit. The next task is the open issue labelled `status:ready` with exactly one lane label, not `needs:human`, not `type:epic`. Order is milestone due date, then issue number.

Lane 1 and lane 2 start a cloud session (Opus 5.5 xhigh, Sonnet 5.5 high). Lane 3 runs `.github/workflows/lane3-implement.yml` (GLM through Z.AI, free, no credit estimate). Every session follows `.claude/commands/implement-issue.md`.

Caps: at most 6 cloud sessions per calendar day (UTC). Estimated spend plus the lane's target must stay at or under $225 of the $250 credit. Targets are $11 for Lane 1 and $3.75 for Lane 2. There is no programmatic way to read the real credit balance. The owner posts `balance $X` on the Credit Ledger issue, and estimated spend resets to 250 minus X.

`.github/workflows/auto-merge.yml` turns on GitHub auto-merge (squash) for `claude/*` pull requests from this repo. GitHub merges only once required checks pass and conversations are resolved. This depends on branch protection with required checks on `main`.

The off switch is the repo variable `AUTOPILOT`, which must equal `on`. `scripts/autopilot/autopilot on|off` sets it.

The dispatcher labels the issue `needs:human`, posts a ledger note and pauses when:

- the pull request is labelled `needs:human`;
- a required check has been failing for over an hour;
- the pull request is still unmerged two hours after it opened;
- no pull request exists three hours after launch.

The escalated issue leaves the slot, and a failed launch pauses too, without the label. The dispatcher stays paused until the owner runs `autopilot on`. The owner also posts the balance. Only a person removes `needs:human`.

## Consequences

Spend is an estimate between balance posts. The Mac must be on and logged in. Every decision is one line in `~/.cograil/autopilot.log`, and every start and finish is a comment on the Credit Ledger issue. New workflows take effect only after they are merged.

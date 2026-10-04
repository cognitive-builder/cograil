# ADR 0014: Proportionate Testing in Build Sessions

Status: Proposed · Date: 2026-10-03 · Discipline: Harness Engineering, applied to the build itself

## Context

Coding agents tend to over-test: repeated full-suite runs, end-to-end runs for small changes, and long logs read back into an expensive context. Build sessions are paid from a fixed credit, while GitHub runs every test for free on each pull request.

## Decision

Three layers, the same shape as Cograil's own rails under the model:

1. **Rules.** `CLAUDE.md` (Testing Budget) sets how much testing each kind of change deserves and how tests are written.
2. **Rails.** A PreToolUse hook (`.claude/hooks/test_budget.py`) counts test runs per session and blocks runs beyond `.claude/test-budget.json`: 20 targeted, 5 repeats of one command, 2 full, 1 end-to-end, 0 real-model. Limits are read from `origin/main`, so a session cannot loosen its own budget. Test defaults are quiet, stop after three failures and cap each test at 60 seconds. `scripts/check.sh` is the one test command and shows output only for failing steps.
3. **Safety net.** GitHub runs the full unit suite, integration and end-to-end tests on every pull request, without the session defaults.

## Consequences

Sessions spend their credit on building, not re-running. Every test run in a chained command counts, loops and repeat modes are refused, and an unreadable tally blocks instead of resetting. A crash in the guard lets commands through rather than blocking work, and its own tests run in CI. Changes to the guard or its limits arrive only through reviewed pull requests, and settings changes apply to new sessions only.

The guard reads command text, so it is a fence against accidental overuse, not a security boundary. It cannot see a test configuration edited to select other tests, a deleted tally file, or test options set in an earlier command. Those would be deliberate workarounds, which `CLAUDE.md` forbids and which the reviewer helper and CodeRabbit are positioned to notice. Cloud environments for this repo are not given a model API key, so real-model tests have nothing to call even if the guard were bypassed; keep it that way.

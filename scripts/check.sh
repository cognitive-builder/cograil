#!/usr/bin/env bash
# The one test command for build sessions: proportionate, quiet, capped output (ADR 0014).
#   scripts/check.sh quick   default: format, lint and types; tests matching the changed files
#   scripts/check.sh full    the whole repo: format, lint, types and the full unit suite
#   scripts/check.sh e2e     end-to-end tests only; GitHub also runs them on every pull request
set -o pipefail
mode="${1:-quick}"
lines="${CHECK_MAX_LINES:-40}"
cd "$(git rev-parse --show-toplevel)" || exit 1

cap() { tail -n "$lines"; }
status=0
step() {  # step <label> <command...>: run quietly, show the tail only on failure
  out="$("${@:2}" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ] || { [ "$1" = "tests" ] && [ "$rc" -eq 5 ]; }; then
    echo "ok    $1"
  else
    echo "FAIL  $1"; printf '%s\n' "$out" | cap; status=1
  fi
}

base="$(git merge-base HEAD origin/main 2>/dev/null || git rev-parse HEAD)"
changed() {
  { git diff --name-only --diff-filter=ACMR "$base"; git ls-files --others --exclude-standard; } | sort -u
}

case "$mode" in
  quick)
    py="$(changed | grep -E '\.py$' || true)"
    data="$(changed | grep -E '\.(ya?ml|json|toml)$' || true)"
    if [ -n "$py" ]; then
      # shellcheck disable=SC2086
      step format uv run ruff format --check $py
      # shellcheck disable=SC2086
      step lint uv run ruff check $py
      step types uv run mypy src/
    fi
    if [ -n "$data" ]; then
      # shellcheck disable=SC2086
      step files uv run python -c '
import json, sys, tomllib, yaml
for p in sys.argv[1:]:
    with open(p, "rb") as f:
        if p.endswith(".json"): json.load(f)
        elif p.endswith(".toml"): tomllib.load(f)
        else: yaml.safe_load(f)
' $data
    fi
    tests=""
    for f in $py; do
      case "$f" in
        tests/*) tests="$tests $f" ;;
        src/*) tests="$tests $(find tests -name "test_$(basename "$f" .py)*.py" 2>/dev/null)" ;;
      esac
    done
    tests="$(printf '%s\n' $tests | sort -u | tr '\n' ' ')"
    if [ -n "${tests// /}" ]; then
      # shellcheck disable=SC2086
      step tests uv run pytest $tests
    else
      echo "skip  tests (none match the changed files)"
    fi
    ;;
  full)
    step format uv run ruff format --check .
    step lint uv run ruff check .
    step types uv run mypy src/
    step tests uv run pytest
    ;;
  e2e)
    step tests uv run pytest -m e2e
    ;;
  *)
    echo "usage: scripts/check.sh [quick|full|e2e]"; exit 2 ;;
esac

if [ "$status" -eq 0 ]; then echo "check $mode: PASS"; else echo "check $mode: FAIL"; fi
exit "$status"

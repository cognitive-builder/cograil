#!/usr/bin/env bash
# Bootstrap the Cograil repository on GitHub: labels, milestones, epics, issues, project board, rulesets.
# Prerequisites: gh auth login -h github.com -s project,workflow; the repo already pushed.
# Usage: scripts/bootstrap_github.sh cognitive-builder/cograil
set -euo pipefail

REPO="${1:-cognitive-builder/cograil}"
OWNER="${REPO%%/*}"
TITLE="Cograil Roadmap"

# Refuse before creating anything if gh lacks the project scope (issues are not idempotent).
auth_info="$(gh auth status 2>&1 || true)"
case "$auth_info" in
  *"'project'"*) ;;
  *) echo "gh is missing the 'project' scope. Run: gh auth refresh -h github.com -s project,workflow"; exit 1 ;;
esac
if [ -n "$(gh issue list --repo "$REPO" --state all --limit 1 --json number --jq '.[].number')" ]; then
  echo "$REPO already has issues; this script creates the backlog once. Stopping so nothing is duplicated."; exit 1
fi

echo "== 1. backlog: labels, milestones, epics, issues"
python3 "$(dirname "$0")/create_backlog.py" --repo "$REPO"

echo "== 2. project board"
PROJECT_NUMBER=$(gh project list --owner "$OWNER" --format json --limit 50 \
  | python3 -c "import json,sys; ps=[p for p in json.load(sys.stdin).get('projects',[]) if p['title']=='$TITLE']; print(ps[0]['number'] if ps else '')")
if [ -z "$PROJECT_NUMBER" ]; then
  PROJECT_NUMBER=$(gh project create --owner "$OWNER" --title "$TITLE" --format json | python3 -c "import json,sys; print(json.load(sys.stdin)['number'])")
  echo "created project #$PROJECT_NUMBER"
else
  echo "project #$PROJECT_NUMBER already exists"
fi

echo "== 3. project fields"
gh project field-create "$PROJECT_NUMBER" --owner "$OWNER" --name "Phase" --data-type SINGLE_SELECT \
  --single-select-options "0 Bootstrap,1 Rails,2 Gates and Knowledge,3 Channels and Ops,4 Launch" >/dev/null 2>&1 || true
gh project field-create "$PROJECT_NUMBER" --owner "$OWNER" --name "Size" --data-type SINGLE_SELECT \
  --single-select-options "S,M,L" >/dev/null 2>&1 || true
gh project field-create "$PROJECT_NUMBER" --owner "$OWNER" --name "Model" --data-type SINGLE_SELECT \
  --single-select-options "sonnet,opus" >/dev/null 2>&1 || true
gh project field-create "$PROJECT_NUMBER" --owner "$OWNER" --name "Credit estimate" --data-type NUMBER >/dev/null 2>&1 || true
gh project field-create "$PROJECT_NUMBER" --owner "$OWNER" --name "Credit actual" --data-type NUMBER >/dev/null 2>&1 || true

echo "== 4. add every open issue to the project"
gh issue list --repo "$REPO" --state open --limit 200 --json url --jq '.[].url' | while read -r url; do
  gh project item-add "$PROJECT_NUMBER" --owner "$OWNER" --url "$url" >/dev/null && echo "added $url"
done

echo "== 5. ruleset on main (require PR, CI and review checks)"
gh api -X POST "repos/$REPO/rulesets" --input - >/dev/null << 'JSON' || echo "ruleset not created (see error above); configure it in Settings > Rules"
{
  "name": "main",
  "target": "branch",
  "enforcement": "active",
  "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "pull_request"}],
  "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request", "parameters": {"required_approving_review_count": 0, "dismiss_stale_reviews_on_push": true, "require_code_owner_review": false, "require_last_push_approval": false, "required_review_thread_resolution": true}},
    {"type": "required_status_checks", "parameters": {"strict_required_status_checks_policy": true, "required_status_checks": [{"context": "checks"}, {"context": "review"}]}}
  ]
}
JSON

echo "== 6. repository settings"
gh repo edit "$REPO" --enable-issues --enable-projects --enable-discussions=false --delete-branch-on-merge >/dev/null
gh api -X PUT "repos/$REPO/vulnerability-alerts" >/dev/null 2>&1 || true

cat << 'NEXT'

Done. Next steps (manual, five minutes):
  1. Settings > Secrets and variables > Actions: add ZAI_API_KEY (GLM via Z.AI, the default in the kit) or switch the workflows to CLAUDE_CODE_OAUTH_TOKEN / ANTHROPIC_API_KEY.
  2. Install the Claude GitHub App on this repo: https://github.com/apps/claude  (or run /install-github-app in Claude Code).
  3. Open the project board, enable built-in workflows: "Auto-add to project" and "Item closed -> Done".
  4. claude.ai/code: connect GitHub, pick the repo, paste scripts/cloud_setup.sh into the environment setup script.
  5. Start the first cloud session: /implement-issue <number of "feat: domain model">.
NEXT

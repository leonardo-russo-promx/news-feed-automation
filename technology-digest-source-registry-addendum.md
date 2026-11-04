# Addendum: Source registry as a "living database" (no extra permissions)

Context: Technology Digest (Python 3.12, GitHub Actions). Keep all existing constraints (no secrets, untrusted
content handling, failing sources never stop a run). The registry must need ONLY GITHUB_TOKEN + repo permissions:
no SharePoint, no Graph Sites permissions, no Entra changes.

## Design
- Human-edited config: config/sources.yaml on main (reviewed via PR). Add fields: owner (GitHub handle, optional),
  added_on (date), tc_board_id (optional). Remove/ignore any last_check/last_success/last_error fields from config.
- Add config/sources.schema.json (JSON Schema mirroring the pydantic Source model) and a pydantic validator:
  unique ids (slug pattern ^[a-z0-9-]+$), valid URLs, method-specific required fields
  (rss: feed_url or website_url; learn_search: learn_scope; page: website_url),
  values starting with "TODO" only allowed when enabled: false OR reported as status=skipped.
- Machine-written state lives ONLY on branch `digest-state` (create orphan branch if missing):
  state/source_health.json, state/seen.json, state/pages.json, state/runs.json, STATUS.md, sources.opml.
  source_health.json per id: last_check, last_success, last_error, last_error_at, consecutive_failures,
  items_last_run, items_30d, newest_item_at, resolved_feed_url, etag, last_modified,
  health (healthy|stale|failing|suspended), suspended_since.
- digest.yml: checkout main (config/code) and digest-state into ./state-branch (second actions/checkout with ref),
  run, then commit/push only to digest-state. concurrency group "digest". Never write to main.

## Health rules (configurable in settings.yaml)
- failing: consecutive_failures >= 1. suspended: consecutive_failures >= 3 (suspend_after_failures: 3).
- Suspended sources are excluded from the digest but get one cheap probe per run; on success -> healthy, reset counter.
- stale: feed reachable but newest_item_at older than stale_after_days: 42.
- Use ETag/Last-Modified conditional GETs when available.
- If discovery yields a working feed different from config, store resolved_feed_url, use it, and list
  "Suggested fix: update feed_url" in the health issue.

## Reporting each run
1 Digest footer: counts per health state + links to failing sources.
2 Pinned issue "Source health" (find by label source-health-dashboard; create if missing): rewrite body with a
  markdown table (id, title, health, last_success, items_30d, last_error short).
3 For each newly suspended source: open/update issue "Source suspended: <id>" (label source-health, assign owner
  if set); close it automatically when the source recovers.
4 Regenerate STATUS.md and sources.opml (OPML 2.0, outlines grouped by area, xmlUrl = resolved or configured feed)
  on digest-state; also write the table to $GITHUB_STEP_SUMMARY.

## PR validation workflow (.github/workflows/validate-sources.yml)
on: pull_request (paths: config/sources.yaml, config/sources.schema.json). permissions: contents: read,
pull-requests: write.
- Step 1 (blocking): schema + pydantic validation of the whole file.
- Step 2: diff against base; live-test only added/changed enabled sources via `python -m digest validate-sources
  --only <ids>` (status, item count, newest item date, discovered feed). Added source failing -> fail check;
  changed existing source failing -> warning only.
- Post/update a single PR comment with a results table.

## Issue-ops: add a source without git
- .github/ISSUE_TEMPLATE/add-source.yml (issue form): title, website_url, feed_url (optional), area (dropdown:
  AI, Azure, Development, Developer, Dynamics, Power Platform, Security, License, Roadmap, Microsoft Platform),
  method (dropdown: rss, learn_search, page), reason. Auto-label: source-request.
- .github/workflows/source-request.yml: on issues opened/edited with label source-request; permissions:
  contents: write, pull-requests: write, issues: write. Parse the form body, generate a slug id, run discovery +
  live test, create branch source/<id>, append the entry to sources.yaml (preserve comments/order; use ruamel.yaml),
  open a PR "Add source: <title>" with the test results in the body and "Closes #<issue>", and comment on the issue.
  On invalid input, comment the errors and do not open a PR.
- Document in README: requires repo setting "Allow GitHub Actions to create and approve pull requests"; PRs
  created with GITHUB_TOKEN don't trigger pull_request workflows, so results are in the PR body; a reviewer can
  push a commit or close/reopen to run CI.

## OPML
- `python -m digest export-opml` (used by the weekly run) and optional `python -m digest import-opml <file>`
  that prints YAML entries for new feeds (no automatic write).

## Optional (separate PR): monthly feed suggestions
- .github/workflows/discover.yml (cron monthly + manual): collect outbound links from the last 30 days of items,
  keep domains/paths not in the list, run feed discovery, rank by link count, open one issue
  "Feed suggestions YYYY-MM" with the top 5 and a link to the add-source issue form. Never edits sources.yaml.

## Acceptance criteria
- Editing only sources.yaml via the GitHub web editor + PR is enough to add/disable/fix a source; CI blocks
  invalid YAML and new sources whose feed doesn't load.
- Weekly runs never commit to main; all runtime fields live on digest-state; no merge conflicts between humans
  and the workflow.
- A source failing 3 consecutive runs becomes suspended, gets an issue, is excluded from the digest, and is
  auto-restored on success.
- The pinned "Source health" issue, STATUS.md and the digest footer show the same health counts.
- Tests: schema validation (valid/invalid fixtures), health state transitions, OPML output snapshot,
  issue-form body parsing, sources.yaml append preserving comments. No network in unit tests.
- Only GITHUB_TOKEN is used for registry features; no new secrets or Entra permissions.

# Task: Build "Technology Digest" – a weekly research automation (Python, GitHub Actions)

## Goal
Every week, collect updates from the past 7 days across a fixed list of Microsoft-related blogs, roadmaps,
docs pages, a licensing page and a newsletter; have an LLM rate and summarize them; and deliver a
digest to the whole team as (a) an easy-to-read HTML email and (b) an HTML archive page. The digest starts
with a management summary (max 7 bullets) and 3–5 calls to action (e.g. "Look into feature X"), followed
by sections per technology area. Each item has a short summary, release status, suggested action and a link.

## Hard constraints
- Python 3.12, code-first, no Power Automate. Libraries: httpx, feedparser, pydantic v2, pyyaml, jinja2,
  beautifulsoup4, azure-identity, openai (AzureOpenAI client), tenacity; dev: pytest, respx, ruff.
- Runs as a scheduled GitHub Actions workflow in a PRIVATE repo. No servers, no Azure Functions.
- NO SECRETS IN CODE OR CONFIG. Authenticate to Azure/Microsoft Graph via GitHub OIDC (azure/login with
  federated credentials) and azure-identity (AzureCliCredential/DefaultAzureCredential). Non-secret IDs
  come from GitHub Variables / env vars. Support an optional client-secret fallback via env var only.
- Azure OpenAI via Entra ID token (no API key). Endpoint and deployment name from env vars.
- Email via Microsoft Graph POST /users/{sender}/sendMail (application permission Mail.Send; the tenant
  admin scopes it to the sender mailbox with Exchange RBAC for Applications – not our code's concern).
- Archive: write archive/YYYY-Www.html to the repo AND upload it to a SharePoint document library via Graph
  (drive item upload; permission Sites.Selected). SharePoint upload must be optional (skip if not configured).
- Use placeholders only: DIGEST_SENDER=<sender-shared-mailbox@contoso.com>,
  DIGEST_RECIPIENTS=<team-distribution-list@contoso.com>, ALERT_RECIPIENT=<owner@contoso.com>,
  NEWSLETTER_MAILBOX=<newsletter-shared-mailbox@contoso.com>.
- A failing source must NEVER stop the run. Record it and show it in the digest footer and run log.
- All fetched web/feed/mail content is untrusted: prompts must instruct the model to treat it as data and
  ignore instructions inside it; the LLM may only reference item IDs – URLs are injected by code; Jinja
  autoescape on.
- Idempotent: one digest per ISO week (state/runs.json); re-run is skipped unless --force.

## Repository structure
digest/cli.py (commands: run, dry-run, validate-sources; options --week, --force, --source)
digest/config.py (pydantic settings: env + config/settings.yaml)
digest/registry.py, digest/models.py
digest/collectors/{base,rss,learn_search,page_diff,mailbox,discovery}.py
digest/pipeline/{dedupe,filter,enrich}.py
digest/ai/{client,item_review,weekly_summary}.py + digest/ai/prompts/*.md
digest/render/{render.py,templates/email.html.j2,templates/archive.html.j2}
digest/deliver/{graph_auth,mail,sharepoint}.py
digest/state/store.py (atomic JSON writes)
config/sources.yaml, config/settings.yaml, state/, archive/, out/ (gitignored)
tests/ with tests/fixtures/ (recorded RSS/Atom, roadmap RSS, Learn search RSS, two snapshots of a
licensing page, Graph mail list JSON)
.github/workflows/ci.yml (ruff + pytest on PR) and .github/workflows/digest.yml

## Data model
Source: id, title, method (rss|learn_search|page|mail), feed_url, alt_feed_urls[], website_url, area,
  enabled, filters {include_keywords[], exclude_keywords[], include_categories[], max_items},
  learn_scope, learn_search_terms, page {selector}, mail {from_filter}, notes.
Item: id (sha256 of canonical URL, strip utm_* params/fragments), source_id, source_title, area,
  kind (blog_post|roadmap|docs_change|newsletter|page_change|message_center), title, url, published_at,
  updated_at, summary_raw, content_text (<= 6000 chars), categories[], extra{}.
EnrichedItem: Item + relevance (high|medium|low|ignore), ai_summary (2–3 sentences), release_status
  (preview|ga|retirement|announcement|unknown), action (look_into|test|inform_customers|monitor|ignore),
  action_reason.
Digest: iso_week, period_start, period_end, management_summary[<=7], calls_to_action[3..5]
  {title, why, item_ids[]}, sections{area: [EnrichedItem]}, stats, failed_sources[].
SourceResult: source_id, status (ok|empty|skipped|error), item_count, error, duration_ms.

## Pipeline
1 collect (concurrent, per-source try/except, timeouts, 2 retries with backoff, User-Agent set)
2 window: published/updated within lookback_days (7; learn_search uses 14)
3 dedupe by canonical URL within run and against state/seen.json (prune entries > 120 days)
4 filter per source (keywords/categories, max_items); global cap max_items_llm=80 (prefer newest)
5 enrich: fetch article main text for remaining items (bs4, cap length)
6 AI per item: JSON-schema structured output, validate with pydantic, 1 retry, fallback to raw summary
7 AI weekly summary from enriched items (only item IDs allowed; validate IDs exist)
8 render email (inline CSS, table layout, no JS, <100 KB, top 15 items by relevance + link to archive)
  and full archive page (all items grouped by area, failed-source list, run stats)
9 deliver: Graph sendMail; archive commit + optional SharePoint upload
10 update state (seen, pages, runs), write out/run.json, GitHub step summary; if any source failed,
   create/update a GitHub issue titled "Digest source problems" (GITHUB_TOKEN, issues: write) and
   email ALERT_RECIPIENT
dry-run: steps 1–8 only, writes out/email.html, out/archive.html, out/run.json; never sends, never
writes state. If AZURE_OPENAI_ENDPOINT is unset in dry-run, use a deterministic fake LLM.

## Collector rules
- rss: feedparser; try feed_url, then alt_feed_urls, then discovery (<link rel="alternate"
  type="application/rss+xml|atom+xml"> on website_url). Handle missing dates (use updated, else skip
  with warning). Tech Community fallback pattern:
  https://techcommunity.microsoft.com/t5/s/gxcuf89792/rss/board?board.id=<BoardId>
- learn_search: build https://learn.microsoft.com/api/search/rss?search=<terms>&locale=en-us&$filter=scopes/any(t: t eq '<scope>')
  (locale required). Items are kind=docs_change ("documentation updated"). Feed returns the 100 most
  recently updated articles and may lag up to a week -> 14-day window + dedupe. If learn_scope starts
  with "TODO", status=skipped ("not configured").
- page: fetch website_url, extract text of page.selector (default "main"), normalize whitespace, compare
  with state/pages.json snapshot; first run = baseline only (no items); on change, compute added text
  blocks and pass them to the LLM to extract dated entries (title, short summary, url if present -> else
  website_url). Changed hash but empty extraction -> warning.
- mail: Graph GET /users/{NEWSLETTER_MAILBOX}/messages (last 7 days, filter by from_filter), one Item per
  message (kind=newsletter, url=first "view in browser" link or website_url, content_text=HTML->text).
  Track processed message IDs in state. If NEWSLETTER_MAILBOX unset -> skipped.

## config/settings.yaml defaults
timezone: UTC; lookback_days: 7; learn_lookback_days: 14; max_items_llm: 80; max_items_email: 15;
report_language: en; schedule documented as Monday 05:00 UTC; roadmap_products: [Microsoft Copilot Studio,
Microsoft 365 Copilot, Power Apps, Power Automate, Power Pages, Microsoft Dataverse,
Dynamics 365 Project Operations]

## Env vars / GitHub Variables (non-secret)
AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT, DIGEST_SENDER,
DIGEST_RECIPIENTS (comma-separated), ALERT_RECIPIENT, NEWSLETTER_MAILBOX, SHAREPOINT_SITE_ID,
SHAREPOINT_DRIVE_ID, SHAREPOINT_FOLDER, ARCHIVE_BASE_URL. Optional secret fallback: AZURE_CLIENT_SECRET.

## Workflow digest.yml
on: schedule cron "0 5 * * 1" + workflow_dispatch (inputs: dry_run bool default true, force bool, week).
permissions: id-token: write, contents: write, issues: write. concurrency: digest (no cancel).
Steps: checkout, setup-python, pip install, azure/login (OIDC, allow-no-subscriptions only if no AOAI
subscription needed), run CLI, upload out/ as artifact, commit state/ and archive/ (skip on dry run).

## config/sources.yaml (initial content – keep ids stable)
sources:
- {id: cat-blog, title: "Copilot Studio CAT Blog", method: rss, feed_url: "https://microsoft.github.io/mcscatblog/feed", alt_feed_urls: ["https://microsoft.github.io/mcscatblog/feed.xml"], website_url: "https://microsoft.github.io/mcscatblog/", area: AI, enabled: true, notes: "Row 1: renamed (was 'CAT BLog', area Developer Tools); feed path unverified"}
- {id: foundry-blog, title: "Microsoft Foundry Blog", method: rss, feed_url: "https://devblogs.microsoft.com/foundry/feed/", website_url: "https://devblogs.microsoft.com/foundry/", area: Azure, enabled: true}
- {id: devblogs-all, title: "Microsoft Dev Blogs (all)", method: rss, feed_url: "https://devblogs.microsoft.com/feed/", website_url: "https://devblogs.microsoft.com/", area: Development, enabled: true, filters: {include_keywords: [Copilot, agent, MCP, Foundry, "Power Platform", Dataverse, "Azure OpenAI", "Visual Studio", ".NET", "GitHub"], max_items: 15}, notes: "Row 3: very broad, superset of foundry feeds -> keyword filter"}
- {id: licensing-news, title: "Microsoft Licensing News", method: page, website_url: "https://www.microsoft.com/en-us/licensing/news", area: License, enabled: true, page: {selector: "main"}, notes: "Row 4: no feed -> page diff"}
- {id: ppweekly, title: "Power Platform Weekly", method: mail, website_url: "https://www.ppweekly.com/", area: "Power Platform", enabled: true, mail: {from_filter: "TODO-newsletter-sender-address"}, notes: "Row 5: newsletter via shared mailbox; area was General"}
- {id: dynamics-blog, title: "Dynamics 365 Blog", method: rss, feed_url: "https://www.microsoft.com/en-us/dynamics-365/blog/feed/", website_url: "https://www.microsoft.com/en-us/dynamics-365/blog/", area: Dynamics, enabled: true}
- {id: powerplatform-blog, title: "Power Platform Blog", method: rss, feed_url: "https://www.microsoft.com/en-us/power-platform/blog/feed/", website_url: "https://www.microsoft.com/en-us/power-platform/blog/", area: "Power Platform", enabled: true}
- {id: tc-copilot-blog, title: "Microsoft Copilot Blog (Tech Community)", method: rss, feed_url: null, website_url: "https://techcommunity.microsoft.com/category/microsoft-copilot/blog/microsoft-copilot-blog", area: AI, enabled: true, notes: "Row 8: use discovery; else board RSS pattern, board id TODO"}
- {id: tc-copilot-studio-blog, title: "Copilot Studio Blog (Tech Community)", method: rss, feed_url: null, website_url: "https://techcommunity.microsoft.com/category/microsoft-copilot/blog/copilot-studio-blog", area: AI, enabled: true, notes: "Row 9: use discovery; else board RSS pattern, board id TODO"}
- {id: msft-copilot-blog, title: "Microsoft Copilot Blog (microsoft.com)", method: rss, feed_url: "https://www.microsoft.com/en-us/copilot/blog/feed/", website_url: "https://www.microsoft.com/en-us/copilot/blog/", area: AI, enabled: true, notes: "Row 10: was misnamed 'AI at Work'; consumer-oriented"}
- {id: security-blog, title: "Microsoft Security Blog", method: rss, feed_url: "https://www.microsoft.com/en-us/security/blog/feed/", website_url: "https://www.microsoft.com/en-us/security/blog/", area: Security, enabled: true, filters: {max_items: 10}}
- {id: github-product-news, title: "GitHub Blog – Product News", method: rss, feed_url: "https://github.blog/news-insights/product-news/feed/", alt_feed_urls: ["https://github.blog/feed/"], website_url: "https://github.blog/news-insights/product-news/", area: Developer, enabled: true, notes: "Row 12: feed/website mismatch fixed; category feed unverified"}
- {id: foundry-whats-new, title: "Foundry Blog – What's new", method: rss, feed_url: "https://devblogs.microsoft.com/foundry/category/whats-new/feed/", website_url: "https://devblogs.microsoft.com/foundry/category/whats-new/", area: Azure, enabled: false, notes: "Row 13: subset of foundry-blog -> disabled"}
- {id: ai-at-work-roadmap, title: "AI at Work Roadmap", method: rss, feed_url: "https://www.microsoft.com/releasecommunications/api/v2/m365/rss", website_url: "https://www.microsoft.com/microsoft-365/roadmap", area: Roadmap, enabled: true, filters: {include_categories_from_settings: roadmap_products, max_items: 25}, notes: "Row 14: high volume (~8/day); now also carries Dynamics 365/Power Platform"}
- {id: copilot-studio-whats-new, title: "What's new in Copilot Studio (Learn)", method: page, website_url: "https://learn.microsoft.com/en-us/microsoft-copilot-studio/whats-new", area: AI, enabled: true, page: {selector: "main"}, notes: "Row 15: URL fixed (was product landing page); monthly sections"}
- {id: copilot-studio-release-notes, title: "Copilot Studio release notes (Learn)", method: page, website_url: "https://learn.microsoft.com/en-us/power-platform/released-versions/copilotstudio", area: AI, enabled: true, page: {selector: "main"}, notes: "Added for row 15: per-build release notes"}
- {id: project-operations-docs, title: "Project Operations documentation updates", method: learn_search, website_url: "https://learn.microsoft.com/en-us/dynamics365/project-operations/", area: Dynamics, enabled: true, learn_scope: "TODO-get-from-Learn-search-RSS-link", learn_search_terms: "", notes: "Row 16: URL fixed; area was empty"}
- {id: message-center, title: "Microsoft 365 Message Center (community mirror)", method: rss, feed_url: "https://mc.merill.net/rss.xml", website_url: "https://mc.merill.net/", area: "Microsoft Platform", enabled: true, notes: "Row 17: community mirror; later replace with Graph serviceAnnouncement (ServiceMessage.Read.All)"}

## AI prompts (store as markdown files)
item_review: role = technology analyst for a Microsoft Dynamics 365 / Power Platform / Copilot consulting
team; input = one item (title, source, area, text); output JSON per schema; relevance reflects impact on
consulting/delivery work; never invent facts not in the text; release_status=unknown if unclear.
weekly_summary: input = enriched items with IDs (exclude relevance=ignore); output management_summary
(<=7 bullets, plain language, cite item IDs), calls_to_action (3–5, concrete, each with item_ids).

## Work in small PRs, in this order (one issue each)
0 skeleton+CI+settings+sources.yaml validation · 1 validate-sources command (+ discovery) ·
2 RSS collector + models · 3 dedupe/window/filters/state store · 4 learn_search + page_diff ·
5 enrichment · 6 AI item review · 7 AI weekly summary · 8 templates + dry-run ·
9 Graph mail + SharePoint upload · 10 digest workflow + state commit + alert issue ·
11 newsletter mailbox collector · 12 (later) Message Center via Graph.

## Definition of done (whole project)
- `python -m digest dry-run` produces out/email.html and out/archive.html from fixtures with no network
  and no credentials (fake LLM), and all tests pass in CI.
- `python -m digest validate-sources` prints per-source status, newest item date and 30-day count.
- A failing/blocked source produces status=error and appears in the digest footer; the run still sends.
- Second run in the same ISO week is skipped unless --force; no item appears in two digests.
- Email renders in Outlook desktop/web/mobile (tables + inline CSS), < 100 KB, summary + CTAs on top,
  every item links to its source.
- No secrets, tokens or real email addresses committed; README documents required Entra app permissions
  (Mail.Send, Mail.Read scoped via Exchange RBAC for Applications; Sites.Selected; Azure OpenAI
  "Cognitive Services OpenAI User"), GitHub Variables, and how to run locally.
- Open decisions are config values with the defaults above; do not block on them.

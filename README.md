# Technology Digest

Weekly Microsoft technology research digest built with Python 3.12 and GitHub Actions. The workflow collects configured sources, summarizes relevant updates with a model hosted on Azure AI Foundry, and publishes an HTML archive to GitHub Pages.

## Local development

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m digest validate-sources
python -m digest dry-run
pytest
ruff check .
```

The dry run reads recorded items from `tests/fixtures/items.json`, does not make network requests or write state, and uses a deterministic summary when Foundry is not configured. Generated output is written to `out/archive.html` and `out/run.json`.

## GitHub configuration

Azure AI Foundry is the only external dependency. Configure:

- Repository **secrets**: `FOUNDRY_ENDPOINT` (OpenAI-compatible base URL, for example `https://<resource>.openai.azure.com/openai/v1/`), `FOUNDRY_MODEL` (deployment name), and `FOUNDRY_API_KEY`.
- **Settings > Pages > Source**: GitHub Actions.

The `digest` workflow writes weekly archives (`YYYY-Www.html`) and an `index.html` to the `digest-state` branch, then deploys them to GitHub Pages. Pages sites are public on GitHub Free/Pro; use GitHub Enterprise Cloud for private access. Source-health failures are reported through the "digest problems" GitHub issue. Registry workflows use only `GITHUB_TOKEN`. Never commit credentials or tokens.

### Troubleshooting Foundry errors

Each live run starts with a one-call preflight. If Foundry returns 401, 403 or 404, the run fails before anything is rendered, committed or deployed, and the error shows the endpoint host and model it used (never the key).

- **401 "invalid subscription key or wrong API endpoint"**: `FOUNDRY_API_KEY` does not belong to the resource in `FOUNDRY_ENDPOINT`. Copy both from the same resource (**Foundry portal > deployment > Endpoint/Key**, or **Azure portal > resource > Keys and Endpoint**). Use the OpenAI v1 route, `https://<resource>.openai.azure.com/openai/v1/` (or `https://<resource>.services.ai.azure.com/openai/v1/`). Do not use a Foundry *project* URL (`.../api/projects/...`), because it requires Entra ID instead of a key.
- **404**: `FOUNDRY_MODEL` must be the *deployment* name, not the base model name.

Items whose AI review fails for any other reason are not marked as seen, so the next run retries them. To reprocess items already recorded as seen (for example, after a run that used wrong credentials), dispatch the workflow with `force` and `reset_seen`.

## Registry and runtime state

Human-edited sources live in `config/sources.yaml` on the default branch and are schema- and model-validated on PRs. Runtime state, source health, `STATUS.md`, OPML, and weekly archive files are written only to `digest-state`. The source-health dashboard and suspension issues are maintained with `GITHUB_TOKEN`.

To let the source-request issue form create pull requests, enable the repository setting **Allow GitHub Actions to create and approve pull requests**. The workflow creates the `source-request` label when needed; source-health labels are also created automatically. Pull requests opened with `GITHUB_TOKEN` do not trigger `pull_request` workflows automatically; reviewers can push a commit or close and reopen the PR to run normal CI. The source-request workflow reports its live test result in the PR and issue.

The weekly schedule is Monday at 05:00 UTC. Manual dispatch defaults to dry-run; use `force` to rerun an ISO week already recorded, or supply `week` in `YYYY-Www` format. Monthly feed suggestions and OPML import are intentionally deferred.

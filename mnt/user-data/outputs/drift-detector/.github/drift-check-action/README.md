# GitHub Action: drift-check

Runs `api-drift-agent` on every pull request that touches the OpenAPI spec or FastAPI source files. Posts a summary comment on the PR and fails the check if `error`-severity drift items are found.

---

## Workflow file location

`.github/workflows/drift-check.yml`

---

## Trigger conditions

```yaml
on:
  pull_request:
    paths:
      - 'openapi.yaml'
      - 'openapi.json'
      - 'src/**/*.py'
      - 'app/**/*.py'
```

Only runs when relevant files change. Does not run on documentation-only PRs.

---

## What the action does

1. Checkout the repo
2. Set up Python
3. Install `api-drift-agent` and its dependencies
4. Run the deterministic pipeline only (no Ollama in CI — agents requiring local models don't work in GitHub-hosted runners)
5. Parse the JSON output
6. Post a PR comment with the drift summary
7. Fail the check if any `error`-severity items exist

The agent layer (LLM reasoning) is disabled in CI. CI only runs the deterministic diff. The `--explain` flag is not passed.

---

## Full workflow YAML

```yaml
name: API Contract Drift Check

on:
  pull_request:
    paths:
      - 'openapi.yaml'
      - 'openapi.json'
      - 'src/**/*.py'
      - 'app/**/*.py'

jobs:
  drift-check:
    runs-on: ubuntu-latest
    permissions:
      pull-requests: write
      contents: read

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'
          cache: 'pip'

      - name: Install drift-agent
        run: pip install api-drift-agent

      - name: Run drift check
        id: drift
        run: |
          drift-check \
            --spec openapi.yaml \
            --src ./app \
            --output-format json \
            --output-file drift-report.json \
            --exit-code
        continue-on-error: true

      - name: Post PR comment
        uses: actions/github-script@v7
        with:
          script: |
            const fs = require('fs');

            let report;
            try {
              report = JSON.parse(fs.readFileSync('drift-report.json', 'utf8'));
            } catch (e) {
              core.warning('Could not parse drift report');
              return;
            }

            const errors = report.items.filter(i => i.severity === 'error');
            const warnings = report.items.filter(i => i.severity === 'warning');
            const infos = report.items.filter(i => i.severity === 'info');

            const statusEmoji = errors.length > 0 ? '🔴' : warnings.length > 0 ? '🟡' : '🟢';
            const statusText = errors.length > 0 ? 'Contract drift detected' : warnings.length > 0 ? 'Minor drift detected' : 'No drift detected';

            let body = `## ${statusEmoji} API Contract Drift Check — ${statusText}\n\n`;

            if (report.items.length === 0) {
              body += '_Spec and implementation are in sync._\n';
            } else {
              body += `| Severity | Count |\n|---|---|\n`;
              body += `| 🔴 Error | ${errors.length} |\n`;
              body += `| 🟡 Warning | ${warnings.length} |\n`;
              body += `| ℹ️ Info | ${infos.length} |\n\n`;

              if (errors.length > 0) {
                body += `### Errors (blocking)\n\n`;
                for (const item of errors.slice(0, 10)) {
                  body += `**\`${item.endpoint}\`** → \`${item.location}\`\n`;
                  body += `> ${item.detail}\n\n`;
                }
                if (errors.length > 10) {
                  body += `_...and ${errors.length - 10} more errors_\n\n`;
                }
              }

              if (warnings.length > 0) {
                body += `<details><summary>Warnings (${warnings.length})</summary>\n\n`;
                for (const item of warnings.slice(0, 20)) {
                  body += `**\`${item.endpoint}\`** → \`${item.location}\`\n`;
                  body += `> ${item.detail}\n\n`;
                }
                body += `</details>\n\n`;
              }
            }

            body += `\n---\n_Run \`drift-check --spec openapi.yaml --src ./app --explain\` locally for source-of-truth analysis and patch suggestions._`;

            // Delete previous drift-check comments
            const comments = await github.rest.issues.listComments({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
            });

            for (const comment of comments.data) {
              if (comment.body.includes('API Contract Drift Check')) {
                await github.rest.issues.deleteComment({
                  owner: context.repo.owner,
                  repo: context.repo.repo,
                  comment_id: comment.id,
                });
              }
            }

            await github.rest.issues.createComment({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
              body: body,
            });

      - name: Fail on errors
        if: steps.drift.outcome == 'failure'
        run: |
          echo "Drift check found error-severity contract violations."
          echo "Run 'drift-check --spec openapi.yaml --src ./app --explain' locally to resolve."
          exit 1
```

---

## CLI flags used in CI

**`--output-format json`** — emit machine-readable JSON instead of rich terminal output

**`--output-file drift-report.json`** — write output to file (in addition to stdout)

**`--exit-code`** — exit with code 1 if any `error`-severity items found, 0 otherwise. Without this flag, the tool always exits 0.

These flags must be implemented in `cli.py`.

---

## JSON output format

When `--output-format json` is passed:

```json
{
  "run_id": "abc123",
  "timestamp": "2024-11-17T14:23:00Z",
  "spec_path": "openapi.yaml",
  "src_path": "./app",
  "spec_endpoints": 23,
  "code_endpoints": 27,
  "items": [
    {
      "id": "a3f92c1b4d5e",
      "endpoint": "POST /users",
      "category": "additive_drift",
      "location": "response.201.schema.created_at",
      "detail": "Code returns field 'created_at' (string/date-time) not documented in spec",
      "spec_evidence": null,
      "code_evidence": "created_at: datetime (app/schemas.py:23)",
      "severity": "warning"
    }
  ],
  "summary": {
    "error": 3,
    "warning": 4,
    "info": 1,
    "total": 8
  }
}
```

---

## Configuration file

To avoid long CLI invocations, support a `.drift-check.yml` config file in the project root:

```yaml
# .drift-check.yml
spec: openapi.yaml
src: ./app
patch_dir: ./patches
exit_code: true
ignore:
  - endpoint: "DELETE /internal/*"    # glob pattern
  - category: "ghost_endpoint"
    endpoint: "GET /health"           # ignore health check ghost endpoint
```

CLI flags override config file values. Config file is optional.

# api-drift-agent

`api-drift-agent` is a CLI tool for detecting drift between an OpenAPI 3.x spec and a FastAPI codebase.

It has two layers:

- a deterministic pipeline that parses the spec, analyzes the FastAPI source tree, and computes contract drift
- an optional local agent layer that uses an open-weights model through Ollama to explain drift and suggest patches

## What it does

- Parses OpenAPI YAML/JSON into a normalized contract model
- Statically analyzes FastAPI routes, parameters, request bodies, and response models
- Compares spec vs code and emits structured drift items
- Optionally runs a local reasoning agent with context tools like git history, coverage, and code search
- Generates patch artifacts for the spec and code review flow

## Features

- CLI-first workflow
- Local model support via `Ollama`
- JSON output for CI and automation
- Optional `.drift-check.yml` config file
- Patch preview/apply modes
- GitHub Actions workflow included

## Requirements

- Python `3.11+`
- A FastAPI codebase
- An OpenAPI `3.x` spec file
- For agent mode: a local Ollama instance with `qwen2.5-coder:7b`

## Installation

```bash
pip install -e .[dev]
```

For agent mode:

```bash
ollama pull qwen2.5-coder:7b
```

## Usage

Deterministic drift check only:

```bash
drift-check --spec openapi.yaml --src ./app
```

Explain drift with the local model and generate patch artifacts:

```bash
drift-check --spec openapi.yaml --src ./app --explain --patch-dir ./patches
```

Emit JSON and fail CI on error-severity drift:

```bash
drift-check \
  --spec openapi.yaml \
  --src ./app \
  --output-format json \
  --output-file drift-report.json \
  --exit-code
```

## CLI options

- `--spec` path to the OpenAPI file
- `--src` path to the FastAPI project root
- `--explain` enable the Ollama-backed agent layer
- `--patch-dir` directory for generated patch artifacts
- `--patch-mode` `preview` or `apply`
- `--output-format` `rich` or `json`
- `--output-file` write JSON output to a file
- `--exit-code` exit with code `1` when error-level drift exists
- `--model` Ollama model name, default `qwen2.5-coder:7b`

## Config file

You can define defaults in `.drift-check.yml`:

```yaml
spec: openapi.yaml
src: ./app
patch_dir: ./patches
ignore:
  - endpoint: "GET /health"
    category: "ghost_endpoint"
```

CLI flags override config values.

## Output

The tool produces:

- drift items with severity, category, endpoint, location, and evidence
- optional agent findings with source-of-truth reasoning
- optional patch artifacts in the patch directory

When `--output-format json` is used, the output includes:

- run metadata
- endpoint counts
- drift items
- severity summary
- optional findings and patch report

## Patch generation

With `--explain`, the tool can generate:

- `spec_patched.yaml`
- `code_patch_summary.md`
- `ambiguous.md`

In `apply` mode, code patches are applied only after basic safety checks and syntax validation.

## Project structure

```text
src/drift_agent/
  agent/            # local LLM orchestration
  code_analyzer/    # FastAPI static analysis
  context_tools/    # git, coverage, search, file reads
  diff_engine/      # deterministic contract comparison
  patch_generator/  # spec/code patch generation
  spec_parser/      # OpenAPI parsing and normalization
  cli.py            # Typer CLI entrypoint
  types.py          # shared contract and drift models
```

## Development

Run tests:

```bash
python -m pytest
```

The repo currently includes unit coverage for:

- spec parsing
- code analysis
- drift computation
- patch generation
- basic agent behavior

## Notes

- The deterministic pipeline works without Ollama.
- The agent layer is local-only and intended for use on machines that can run open-weights models.
- Static analysis is best-effort and focuses on common FastAPI and Pydantic patterns.

## Status

Implementation progress is tracked in `WORK_DONE.md`.

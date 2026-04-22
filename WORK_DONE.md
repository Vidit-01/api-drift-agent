# Work Done

## Milestone 1

- Set up the Python package skeleton for `api-drift-agent`.
- Added shared dataclasses in `src/drift_agent/types.py` and project-wide error types in `src/drift_agent/errors.py`.
- Implemented the OpenAPI spec parsing pipeline in `src/drift_agent/spec_parser/parser.py` and `src/drift_agent/spec_parser/resolver.py`.
- Added initial spec parser fixtures and tests in `tests/test_spec_parser.py`.

## Milestone 2

- Implemented the static FastAPI analyzer across `src/drift_agent/code_analyzer/walker.py`, `src/drift_agent/code_analyzer/extractor.py`, and `src/drift_agent/code_analyzer/resolver.py`.
- Added deterministic drift comparison in `src/drift_agent/diff_engine/comparator.py` with stable drift IDs in `src/drift_agent/diff_engine/classifier.py`.
- Added FastAPI app fixtures and unit tests for both the analyzer and diff engine in `tests/test_code_analyzer.py` and `tests/test_diff_engine.py`.

## Milestone 3

- Added deterministic context tools under `src/drift_agent/context_tools/` for git history, coverage lookup, code search, and file slices.
- Implemented the Ollama-backed agent loop in `src/drift_agent/agent/core.py` with fast-path handling, tool dispatch, and JSON retry logic.
- Added preview/apply patch generation in `src/drift_agent/patch_generator/` plus a Typer CLI in `src/drift_agent/cli.py`.
- Added the CI workflow in `.github/workflows/drift-check.yml`.
- Added focused tests for the patch generator and agent behavior in `tests/test_patch_generator.py` and `tests/test_agent.py`.

## Milestone 4

- Removed cached/generated artifacts such as `__pycache__`, `.pytest_cache`, and editable-install metadata.
- Removed the copied reference scaffold under `mnt/` after using it to implement the project, keeping the repo focused on the actual source, tests, and project docs.

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

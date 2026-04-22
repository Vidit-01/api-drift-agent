# api-drift-agent

An agentic CLI tool that detects drift between an OpenAPI 3.x specification and a FastAPI codebase, infers which side is the source of truth, and generates reconciliation patches.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        AGENT CORE                            │
│   Qwen2.5-Coder-7B via Ollama — orchestrates all tools      │
│   Decides tool call order, interprets results, iterates      │
└───────────────────────┬─────────────────────────────────────┘
                        │ structured tool calls
         ┌──────────────┼──────────────────────┐
         ▼              ▼                       ▼
   spec_parser     code_analyzer          context_tools
   (OpenAPI 3.x)   (Python AST)           (git, coverage, fs)
         │              │                       │
         └──────────────┴───────────────────────┘
                        │
                        ▼
                  diff_engine
                  (deterministic schema comparison)
                        │
                        ▼
                 patch_generator
                 (spec YAML patch + code AST rewrite)
```

The LLM is the decision-making core. Every other component is a deterministic tool the agent invokes. The agent decides which tools to call, in what order, and how to interpret ambiguous results.

---

## Repository structure

```
api-drift-agent/
├── README.md                  ← this file
├── pyproject.toml
├── src/
│   └── drift_agent/
│       ├── __init__.py
│       ├── cli.py             ← typer CLI entrypoint
│       ├── agent/
│       │   ├── README.md      ← agent loop spec
│       │   ├── core.py        ← agent orchestration loop
│       │   ├── tools.py       ← tool registry + dispatch
│       │   └── prompts.py     ← system prompt + tool schemas
│       ├── spec_parser/
│       │   ├── README.md      ← spec parser spec
│       │   ├── parser.py      ← OpenAPI ingestion + normalization
│       │   └── resolver.py    ← $ref resolution + schema flattening
│       ├── code_analyzer/
│       │   ├── README.md      ← code analyzer spec
│       │   ├── walker.py      ← AST traversal + route discovery
│       │   ├── resolver.py    ← Pydantic model resolution
│       │   └── extractor.py   ← parameter + response extraction
│       ├── diff_engine/
│       │   ├── README.md      ← diff engine spec
│       │   ├── comparator.py  ← schema comparison
│       │   └── classifier.py  ← drift category classification
│       ├── context_tools/
│       │   ├── README.md      ← context tools spec
│       │   ├── git.py
│       │   ├── coverage.py
│       │   └── search.py
│       └── patch_generator/
│           ├── README.md      ← patch generator spec
│           ├── spec_patch.py  ← YAML spec patch generation
│           └── code_patch.py  ← AST-based code patch generation
├── tests/
│   ├── fixtures/
│   │   ├── specs/             ← sample OpenAPI YAML files
│   │   └── apps/              ← sample FastAPI apps
│   ├── test_spec_parser.py
│   ├── test_code_analyzer.py
│   ├── test_diff_engine.py
│   └── test_patch_generator.py
└── .github/
    └── workflows/
        └── drift-check.yml    ← GitHub Action
```

---

## Canonical data types

All components communicate via these shared types. Defined in `src/drift_agent/types.py`.

### `NormalizedContract`

The shared intermediate representation that both `spec_parser` and `code_analyzer` produce. The `diff_engine` operates on two `NormalizedContract` objects.

```python
@dataclass
class FieldSchema:
    name: str
    type: str                        # canonical type string: "string", "integer", "boolean",
                                     # "number", "array", "object", "null"
    format: Optional[str]            # "date-time", "email", "uuid", etc.
    required: bool
    nullable: bool
    items: Optional['FieldSchema']   # if type == "array"
    properties: Optional[Dict[str, 'FieldSchema']]  # if type == "object"
    enum: Optional[List[Any]]
    description: Optional[str]


@dataclass
class ParameterSchema:
    name: str
    location: Literal["path", "query", "header", "cookie"]
    required: bool
    schema: FieldSchema


@dataclass
class RequestBodySchema:
    required: bool
    content_type: str                # "application/json", "multipart/form-data", etc.
    schema: FieldSchema              # always type == "object" at top level


@dataclass
class ResponseSchema:
    status_code: str                 # "200", "201", "422", "default"
    content_type: Optional[str]
    schema: Optional[FieldSchema]    # None if no response body (204 etc.)


@dataclass
class EndpointContract:
    method: str                      # uppercase: "GET", "POST", etc.
    path: str                        # normalized: "/users/{user_id}"
    parameters: List[ParameterSchema]
    request_body: Optional[RequestBodySchema]
    responses: Dict[str, ResponseSchema]  # keyed by status code string
    tags: List[str]
    source_file: Optional[str]       # only populated by code_analyzer
    source_line: Optional[int]       # only populated by code_analyzer


@dataclass
class NormalizedContract:
    endpoints: Dict[str, EndpointContract]  # key: "METHOD /path", e.g. "POST /users"
    source: Literal["spec", "code"]
    metadata: Dict[str, Any]         # spec: {title, version, servers}
                                     # code: {app_file, framework_version, python_version}
```

### `DriftItem`

Output of the `diff_engine`. Input to the agent for source-of-truth inference.

```python
@dataclass
class DriftItem:
    id: str                          # deterministic hash of (endpoint, location, category)
    endpoint: str                    # "POST /users"
    category: DriftCategory          # see diff_engine/README.md
    location: str                    # dot-path: "response.201.schema.created_at"
    detail: str                      # human-readable description
    spec_evidence: Optional[str]     # what the spec says, or None if absent
    code_evidence: Optional[str]     # what the code says, or None if absent
    severity: Literal["error", "warning", "info"]


class DriftCategory(Enum):
    MISSING_ENDPOINT    = "missing_endpoint"    # in spec, not in code
    GHOST_ENDPOINT      = "ghost_endpoint"      # in code, not in spec
    ADDITIVE_DRIFT      = "additive_drift"      # code has field spec doesn't
    DESTRUCTIVE_DRIFT   = "destructive_drift"   # spec has field code doesn't return
    TYPE_DRIFT          = "type_drift"          # field present both sides, types differ
    NULLABILITY_DRIFT   = "nullability_drift"   # nullable semantics differ
    REQUIRED_DRIFT      = "required_drift"      # required/optional semantics differ
    STATUS_CODE_DRIFT   = "status_code_drift"   # different response status codes
    PARAMETER_DRIFT     = "parameter_drift"     # parameter added/removed/renamed
```

### `AgentFinding`

Final output per drift item after agent reasoning.

```python
@dataclass
class AgentFinding:
    drift_item: DriftItem
    source_of_truth: Literal["CODE", "SPEC", "AMBIGUOUS"]
    confidence: Literal["high", "medium", "low"]
    reasoning: str                   # natural language, cites evidence gathered
    evidence: List[Dict[str, Any]]   # raw tool outputs used
    patch: Optional[PatchSpec]       # None if AMBIGUOUS


@dataclass
class PatchSpec:
    target: Literal["spec", "code"]
    patch_type: str                  # "add_field", "remove_field", "change_type", etc.
    location: str                    # where to apply the patch
    content: str                     # the actual patch content (YAML fragment or Python AST)
```

---

## Dependencies

```toml
[tool.poetry.dependencies]
python = "^3.11"
typer = "^0.12"
rich = "^13"
pyyaml = "^6"
jsonschema = "^4"
ollama = "^0.2"          # ollama Python client
gitpython = "^3"
libcst = "^1.3"          # concrete syntax tree for code patches (preserves formatting)
coverage = "^7"
pytest = "^8"
```

Use `libcst` not `ast` for code patch generation. `ast` does not preserve comments, formatting, or whitespace when unparsing. `libcst` is a lossless CST and produces patches that don't destroy the file.

---

## Running locally

```bash
# prerequisites: ollama running with qwen2.5-coder:7b pulled
ollama pull qwen2.5-coder:7b

pip install -e .

drift-check --spec openapi.yaml --src ./app
drift-check --spec openapi.yaml --src ./app --explain    # enables agent layer
drift-check --spec openapi.yaml --src ./app --patch-dir ./patches
```

Without `--explain`, the tool runs only the deterministic pipeline (spec parser + code analyzer + diff engine) and outputs a structured drift report. No LLM required. With `--explain`, the agent layer activates.

---

## Output format

```
DRIFT REPORT  openapi.yaml ↔ ./app
──────────────────────────────────────────────────────────────

[ERR] POST /users → response.201.schema → ADDITIVE DRIFT
      Code returns : created_at (string/date-time)
      Spec documents: (absent)
      Source of truth: CODE  [confidence: high]
      Reason: Committed 3d ago (a3f92c1 "add created_at to response"), test coverage present
      Patch → spec: add created_at: {type: string, format: date-time} to UserResponse

[ERR] GET /users/{id} → parameters → PARAMETER DRIFT
      Code expects : user_uuid (path, string/uuid)
      Spec documents: user_id (path, integer)
      Source of truth: CODE  [confidence: medium]
      Reason: Parameter renamed in commit b2e41a0 2w ago, spec not updated
      Patch → spec: rename user_id → user_uuid, change type integer → string/uuid

[WARN] DELETE /users/{id} → GHOST ENDPOINT
       In code, not in spec
       Source of truth: AMBIGUOUS  [confidence: low]
       Reason: No recent commits, no test coverage, not referenced internally
       Action: Manual review required

──────────────────────────────────────────────────────────────
8 drift items  |  5 auto-resolved  |  3 ambiguous
Patches written: ./patches/spec_patch.yaml, ./patches/code_patch.py
```

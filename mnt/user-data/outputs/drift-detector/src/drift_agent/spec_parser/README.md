# spec_parser

Ingests an OpenAPI 3.x specification file and produces a `NormalizedContract` object. No LLM involvement. Fully deterministic.

---

## Responsibilities

1. Load and validate the OpenAPI document (YAML or JSON)
2. Fully resolve all `$ref` references (including circular refs)
3. Flatten `allOf`, `anyOf`, `oneOf` compositions into canonical `FieldSchema` objects
4. Normalize type strings to the canonical set used by `diff_engine`
5. Extract all endpoints into `EndpointContract` objects
6. Return a `NormalizedContract` with `source="spec"`

---

## Input

A file path to an OpenAPI 3.0.x or 3.1.x document. YAML or JSON.

Minimum valid document:
```yaml
openapi: "3.0.3"
info:
  title: My API
  version: "1.0.0"
paths:
  /users:
    post:
      requestBody: ...
      responses: ...
```

---

## Output

`NormalizedContract` as defined in the root `README.md`. Every field must be populated. Fields with no spec equivalent use these defaults:

| Field | Default |
|---|---|
| `ParameterSchema.required` | `False` unless `in: path` (path params always required) |
| `FieldSchema.nullable` | `False` unless `nullable: true` (3.0.x) or `type: ["string", "null"]` (3.1.x) |
| `FieldSchema.format` | `None` |
| `ResponseSchema.content_type` | `"application/json"` |

---

## `$ref` resolution

OpenAPI specs use JSON References (`$ref`) heavily. All refs must be resolved before the contract is built. Resolution rules:

**Internal refs** — `$ref: '#/components/schemas/User'`
Walk the document tree using the JSON Pointer path. Cache resolved schemas by pointer to handle repeated references efficiently.

**Circular refs** — `User` → `Address` → `User`
Detect cycles by tracking the ref path on the resolution stack. When a cycle is detected, insert a sentinel `FieldSchema(type="object", description="[circular ref: {pointer}]")` and stop recursing. Do not raise an error.

**External refs** — `$ref: './other.yaml#/components/schemas/Foo'`
V1: Raise `UnsupportedFeatureError` with a descriptive message. Log as warning and skip the endpoint that contains the external ref. Do not crash.

Resolution must be complete before any normalization step. The output of this step is a fully dereferenced document tree with no `$ref` keys remaining anywhere.

---

## Schema composition flattening

### `allOf`

Merge all subschemas into a single object schema. Property conflicts (same key, different types) are flagged as `ConflictError` and the first definition wins.

```yaml
# input
allOf:
  - $ref: '#/components/schemas/Base'
  - properties:
      extra_field:
        type: string

# output (after Base is resolved to {id: integer, name: string})
properties:
  id: {type: integer}
  name: {type: string}
  extra_field: {type: string}
```

### `anyOf` / `oneOf`

These cannot be deterministically flattened. Behavior:
- If all variants have the same type → use that type, mark `nullable: True` if one variant is `{type: null}`
- If variants are a single real type + null → unwrap to that type with `nullable: True`
- Otherwise → produce `FieldSchema(type="object", description="[anyOf/oneOf: unresolvable]")` and emit a warning

```yaml
# resolvable case
anyOf:
  - type: string
  - type: "null"
# → FieldSchema(type="string", nullable=True)

# unresolvable case
anyOf:
  - $ref: '#/components/schemas/Cat'
  - $ref: '#/components/schemas/Dog'
# → FieldSchema(type="object", description="[anyOf: unresolvable]")
```

---

## Type normalization

Map all OpenAPI types to the canonical type strings used throughout the system:

| OpenAPI type | Canonical type |
|---|---|
| `string` | `string` |
| `integer` | `integer` |
| `number` | `number` |
| `boolean` | `boolean` |
| `array` | `array` |
| `object` | `object` |
| `null` (3.1.x) | `null` |
| (absent, has `properties`) | `object` |
| (absent, has `items`) | `array` |
| (absent, no structure) | `unknown` |

Format strings are passed through as-is. Common formats: `date-time`, `date`, `time`, `email`, `uuid`, `uri`, `byte`, `binary`, `int32`, `int64`, `float`, `double`.

String formats that encode specific semantic types (e.g. `email`, `uuid`) are preserved in `FieldSchema.format` but do not change `FieldSchema.type` (remains `string`).

---

## Path normalization

All paths are stored in their exact OpenAPI form: `/users/{user_id}`. No trailing slashes. Parameter names in braces are kept as-is.

Endpoint keys in `NormalizedContract.endpoints` are `"{METHOD} {path}"` with method uppercased: `"GET /users/{user_id}"`.

---

## Error handling

| Condition | Behavior |
|---|---|
| File not found | Raise `SpecLoadError` |
| Not valid YAML/JSON | Raise `SpecParseError` with line number if available |
| Not valid OpenAPI (missing `openapi`, `info`, `paths`) | Raise `SpecValidationError` |
| OpenAPI 2.x (Swagger) | Raise `UnsupportedVersionError` — only 3.x supported |
| `$ref` target not found | Log warning, skip the field containing the ref, continue |
| External `$ref` | Log warning, skip the endpoint, continue |
| `allOf` property conflict | Log warning, first definition wins |
| `anyOf`/`oneOf` unresolvable | Emit sentinel schema, log warning, continue |

Never raise on recoverable parse issues. Always continue and flag. The agent should receive a complete (possibly partial) contract, not a crash.

---

## Implementation notes

**Use `pyyaml` with `yaml.safe_load`** — never `yaml.load`. The spec may come from untrusted sources.

**Do not use the `openapi-spec-validator` package for normalization** — it validates but doesn't produce the normalized IR we need. Use it only for initial validation if desired.

**Resolve refs before touching paths** — do a single DFS ref resolution pass on the raw document first, producing a fully inline document. Then extract endpoints from the inline document. This is simpler than trying to resolve refs lazily during extraction.

**Cache resolved schemas** — use a dict keyed by JSON Pointer string. This matters for large specs with many references to the same schema.

---

## Tests

Test fixtures are in `tests/fixtures/specs/`. Required test cases:

```
specs/
  simple.yaml           # 3 endpoints, no $refs, all primitive types
  with_refs.yaml        # heavy $ref usage, deeply nested
  circular_refs.yaml    # User → Address → User circular dependency
  allof_composition.yaml
  anyof_oneof.yaml
  nullable_fields.yaml  # both 3.0.x nullable:true and 3.1.x type:["string","null"]
  path_params.yaml      # various path parameter patterns
  no_request_body.yaml  # GET-only endpoints
  empty_responses.yaml  # 204 No Content responses
```

Every test asserts on the exact `NormalizedContract` structure, not on string output.

---

## Module interface

```python
from drift_agent.spec_parser import parse_spec

contract: NormalizedContract = parse_spec("openapi.yaml")
# raises SpecLoadError, SpecParseError, SpecValidationError, UnsupportedVersionError
```

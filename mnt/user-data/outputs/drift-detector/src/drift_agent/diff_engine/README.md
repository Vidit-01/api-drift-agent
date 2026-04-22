# diff_engine

Compares two `NormalizedContract` objects (one from `spec_parser`, one from `code_analyzer`) and produces a list of `DriftItem` objects. Fully deterministic. No LLM involvement.

---

## Responsibilities

1. Compare endpoint sets between spec and code
2. For endpoints present in both, recursively compare schemas
3. Classify each difference into a `DriftCategory`
4. Assign severity
5. Generate a deterministic, stable `id` for each drift item
6. Return `List[DriftItem]`

---

## Input

```python
spec_contract: NormalizedContract   # source="spec"
code_contract: NormalizedContract   # source="code"
```

---

## Output

```python
List[DriftItem]
```

Empty list means no drift detected.

---

## Comparison algorithm

### Step 1: Endpoint set comparison

```python
spec_endpoints = set(spec_contract.endpoints.keys())   # {"GET /users", "POST /users", ...}
code_endpoints = set(code_contract.endpoints.keys())

missing = spec_endpoints - code_endpoints    # in spec, not in code
ghost   = code_endpoints - spec_endpoints    # in code, not in spec
shared  = spec_endpoints & code_endpoints    # in both → deep comparison
```

For each `missing` endpoint → emit `DriftItem(category=MISSING_ENDPOINT, severity="error")`.
For each `ghost` endpoint → emit `DriftItem(category=GHOST_ENDPOINT, severity="warning")`.
For each `shared` endpoint → proceed to Step 2.

### Step 2: Parameter comparison

For each shared endpoint, compare `parameters` lists:

Match parameters by `(name, location)` tuple. Three cases:

- In spec not in code → `PARAMETER_DRIFT`, `severity="error"`
- In code not in spec → `PARAMETER_DRIFT`, `severity="warning"` (undocumented param)
- In both → compare schemas via Step 4 field comparison logic

### Step 3: Request body comparison

If spec has a request body but code doesn't → `DESTRUCTIVE_DRIFT`, `severity="error"`.
If code has a request body but spec doesn't → `ADDITIVE_DRIFT`, `severity="warning"`.
If both have a request body → compare `schema` via recursive field comparison (Step 4).

Also compare `content_type`. Mismatch → `PARAMETER_DRIFT`, `severity="warning"`.

### Step 4: Response schema comparison

Compare `responses` dicts. Match by status code string.

Status codes in spec not in code → `STATUS_CODE_DRIFT`, `severity="warning"`.
Status codes in code not in spec → `STATUS_CODE_DRIFT`, `severity="info"` (extra status codes are often fine).

For matching status codes, recursively compare schemas.

**Skip 422 comparison** — FastAPI auto-generates 422 responses. If code has 422 and spec doesn't (or vice versa), do not emit a drift item.

### Step 5: Recursive schema comparison

```python
def compare_schemas(
    spec_schema: Optional[FieldSchema],
    code_schema: Optional[FieldSchema],
    location: str,          # dot-path for the drift item location
    endpoint: str,
) -> List[DriftItem]:
```

**Both None:** no drift.

**Spec has schema, code has None:** `DESTRUCTIVE_DRIFT` at `location`. This means the spec documents a response body but the code returns nothing (or schema is unresolvable). `severity="error"`.

**Code has schema, spec has None:** `ADDITIVE_DRIFT` at `location`. `severity="warning"`.

**Both have schemas — type comparison:**

1. If types differ → `TYPE_DRIFT`. `severity="error"`.
2. If types match but both are `object` → recurse into properties (Steps 5a, 5b).
3. If types match but both are `array` → recurse into `items`.
4. If types match and both are primitives → check format, nullable, required (Steps 5c, 5d, 5e).

**Step 5a: Object property set comparison**

```python
spec_props = set(spec_schema.properties.keys())
code_props = set(code_schema.properties.keys())

missing_in_code = spec_props - code_props   → DESTRUCTIVE_DRIFT per field, severity="error"
extra_in_code   = code_props - spec_props   → ADDITIVE_DRIFT per field, severity="warning"
shared_props    = spec_props & code_props   → recurse
```

**Step 5b: Required field comparison**

For each shared property, if `spec_schema.properties[k].required != code_schema.properties[k].required`:
→ `REQUIRED_DRIFT`, `severity="warning"`

**Step 5c: Nullability comparison**

If `spec_schema.nullable != code_schema.nullable`:
→ `NULLABILITY_DRIFT`
→ `severity="error"` if spec is nullable=False and code is nullable=True (code may return null where spec says it won't)
→ `severity="warning"` if spec is nullable=True and code is nullable=False (stricter than spec)

**Step 5d: Enum comparison**

If either schema has an `enum` and the other doesn't, or enum values differ:
→ `TYPE_DRIFT`, `severity="warning"`

**Step 5e: Format comparison**

If types match but formats differ (e.g. spec says `string/uuid`, code says `string`):
→ `TYPE_DRIFT`, `severity="info"` — format mismatches are informational since formats are advisory in OpenAPI

---

## Location path format

The `location` field in `DriftItem` uses a dot-separated path:

| What | Location string |
|---|---|
| Endpoint-level (missing/ghost) | `"endpoint"` |
| Path parameter `user_id` | `"parameters.user_id"` |
| Request body top level | `"request_body"` |
| Request body field `name` | `"request_body.schema.name"` |
| Response 201 top level | `"response.201"` |
| Response 201 field `created_at` | `"response.201.schema.created_at"` |
| Nested field `address.street` | `"response.200.schema.address.street"` |

---

## Drift item ID generation

The `id` field must be deterministic and stable across runs. It is a hex string derived from:

```python
import hashlib

def make_drift_id(endpoint: str, location: str, category: DriftCategory) -> str:
    raw = f"{endpoint}|{location}|{category.value}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]
```

Same drift item detected on two runs → same ID. This matters for tracking drift items over time and for the agent to avoid re-processing already-resolved items.

---

## Severity assignment summary

| Category | Severity | Rationale |
|---|---|---|
| `MISSING_ENDPOINT` | error | Spec promises something code doesn't deliver |
| `GHOST_ENDPOINT` | warning | Code exposes undocumented surface |
| `DESTRUCTIVE_DRIFT` | error | Spec promises a field/body that code doesn't return |
| `ADDITIVE_DRIFT` | warning | Code returns undocumented data |
| `TYPE_DRIFT` | error | Type mismatch is a contract violation |
| `NULLABILITY_DRIFT` (spec non-null, code nullable) | error | Code may return null where spec guarantees it won't |
| `NULLABILITY_DRIFT` (spec nullable, code non-null) | warning | Code is stricter than spec |
| `REQUIRED_DRIFT` | warning | Semantic difference in field optionality |
| `STATUS_CODE_DRIFT` (extra in code) | info | Extra codes are usually fine |
| `STATUS_CODE_DRIFT` (missing in code) | warning | |
| `PARAMETER_DRIFT` (in spec, not code) | error | Required by spec, not implemented |
| `PARAMETER_DRIFT` (in code, not spec) | warning | Undocumented parameter |

---

## Type normalization before comparison

Before comparing any two `FieldSchema` objects, normalize their types:

**Equivalent types that should NOT produce drift:**
- `number` (spec) vs `integer` (code) → no drift if code always returns integers (integers are valid numbers). Emit `info`-level note but not a drift item.
- `string` with no format (spec) vs `string` with format (code) → no drift at type level; compare format separately.

**Equivalent nullability representations:**
- Spec: `nullable: true` (OpenAPI 3.0.x)
- Spec: `type: ["string", "null"]` (OpenAPI 3.1.x)
- Code: `Optional[str]`

All three are equivalent. Normalize to `nullable: True` before comparison.

---

## Module interface

```python
from drift_agent.diff_engine import compute_drift

drift_items: List[DriftItem] = compute_drift(spec_contract, code_contract)
```

---

## Tests

Test each drift category in isolation. Tests should compare two hand-crafted `NormalizedContract` objects (not parsed from files) to isolate the diff logic from parsing.

Required test cases:

```python
test_missing_endpoint()           # endpoint in spec, not in code
test_ghost_endpoint()             # endpoint in code, not in spec
test_additive_field()             # extra field in code response
test_destructive_field()          # missing field in code response
test_type_mismatch_primitive()    # integer vs string
test_type_mismatch_nested()       # nested object field type mismatch
test_nullability_mismatch()
test_required_mismatch()
test_parameter_missing()
test_parameter_extra()
test_no_drift()                   # identical contracts → empty list
test_drift_id_stability()         # same input → same IDs across two calls
test_422_ignored()                # 422 status code drift not emitted
```

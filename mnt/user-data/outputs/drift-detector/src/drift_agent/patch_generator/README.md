# patch_generator

Generates concrete, applicable patches from `AgentFinding` objects. Two patch targets: the OpenAPI spec (YAML surgery) and the FastAPI source code (CST rewrite via `libcst`). Patches are written to a configurable output directory.

---

## Responsibilities

1. Accept a list of `AgentFinding` objects
2. For each finding with `source_of_truth != "AMBIGUOUS"`, generate a patch for the non-source-of-truth side
3. Group all spec patches into a single `spec_patch.yaml`
4. Group all code patches into a single `code_patch.py` (CST rewrite script) or apply them directly
5. Write an `ambiguous.md` report for unresolved items
6. Return a `PatchReport` summarizing what was generated

---

## Use `libcst`, not `ast`

All code patches use `libcst` (Concrete Syntax Tree), not Python's `ast` module.

Reason: `ast.unparse()` does not preserve comments, blank lines, string quote style, trailing commas, or any formatting. Applying an `ast`-based patch to a real file produces a rewrite that fails code review. `libcst` is lossless — it transforms only what you tell it to and preserves everything else byte-for-byte.

```python
import libcst as cst

class AddFieldTransformer(cst.CSTTransformer):
    def leave_ClassBody(self, original_node, updated_node):
        # insert new field into Pydantic model class body
        ...
```

---

## Patch types

### `add_field` (spec target)

Add a new property to an existing schema component.

```yaml
# patch instruction
target: spec
patch_type: add_field
location: components/schemas/UserResponse/properties
content: |
  created_at:
    type: string
    format: date-time
```

Implementation: Load the spec YAML, navigate to `location` (dot-path into the YAML tree), insert the content. Use `ruamel.yaml` instead of `pyyaml` for spec patches — `ruamel.yaml` preserves comments and key ordering.

```python
from ruamel.yaml import YAML

yaml = YAML()
yaml.preserve_quotes = True
with open(spec_path) as f:
    spec = yaml.load(f)

# navigate location path
node = spec
for key in location.split("/"):
    node = node[key]

# insert content
node[field_name] = yaml.load(content)
```

### `remove_field` (spec target)

Remove a property from a schema component.

```yaml
target: spec
patch_type: remove_field
location: components/schemas/UserResponse/properties
content: legacy_field
```

Navigate to `location`, delete the key named in `content`. Also check if this field appears in the `required` array of the parent schema — if so, remove it from there too.

### `change_type` (spec target)

Change the type of an existing field.

```yaml
target: spec
patch_type: change_type
location: components/schemas/UserResponse/properties/age
content: |
  type: integer
  nullable: true
```

Navigate to `location`, replace the schema node with `content`.

### `add_endpoint` (spec target)

Add a completely new path + operation to the spec. Used for `GHOST_ENDPOINT` where code is source of truth.

```yaml
target: spec
patch_type: add_endpoint
location: paths
content: |
  /users/{user_id}/avatar:
    get:
      summary: Get user avatar
      parameters:
        - name: user_id
          in: path
          required: true
          schema:
            type: integer
      responses:
        '200':
          description: OK
```

### `add_field` (code target)

Add a field to a Pydantic model.

```yaml
target: code
patch_type: add_field
location: app/schemas.py::UserResponse
content: |
  created_at: datetime
```

Implementation using `libcst`:

```python
class AddFieldToModel(cst.CSTTransformer):
    def __init__(self, class_name: str, field_name: str, field_type: str):
        self.class_name = class_name
        self.field_name = field_name
        self.field_type = field_type
        self._in_target_class = False

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        self._in_target_class = (node.name.value == self.class_name)
        return True

    def leave_ClassDef(self, original_node: cst.ClassDef, updated_node: cst.ClassDef):
        if not self._in_target_class:
            return updated_node
        # construct new AnnAssign node for the field
        new_field = cst.parse_statement(f"    {self.field_name}: {self.field_type}\n")
        # append to class body
        new_body = updated_node.body.with_changes(
            body=[*updated_node.body.body, new_field]
        )
        return updated_node.with_changes(body=new_body)
```

Also check if the field type requires a new import (e.g. adding `datetime` requires `from datetime import datetime`). If the import is missing, add it.

### `change_type` (code target)

Change the type annotation of an existing Pydantic field.

```yaml
target: code
patch_type: change_type
location: app/schemas.py::UserResponse::age
content: Optional[int]
```

Use `libcst` to find the `AnnAssign` node with the matching name inside the class body, then replace its `annotation`.

### `remove_field` (code target)

Remove a field from a Pydantic model. Only generated when code is `AMBIGUOUS` (never auto-removed). This is the most destructive operation — always emit as a suggestion in `ambiguous.md` rather than an auto-applied patch.

---

## Patch application modes

Two modes, set via CLI flag:

**`--patch-mode preview` (default)**

Write patches to the output directory as files. Do not modify any source files.

```
patches/
  spec_patch.yaml       ← complete modified spec
  code_patch_summary.md ← describes code changes with file/line references
  ambiguous.md          ← items requiring manual review
```

For code patches in preview mode: write a `code_patch_summary.md` describing each change in human-readable form with file path, line number, and the exact change. Do not apply them.

**`--patch-mode apply`**

Apply patches directly to source files. Before applying:
1. Check that the target file has no uncommitted changes (via `git status`). If it does, abort that patch and log a warning.
2. Write a backup of the original file to `patches/backups/`.
3. Apply the patch using `libcst` for code, `ruamel.yaml` for spec.
4. Verify the patched file is syntactically valid (parse it back). If not, restore from backup and log an error.

Never apply patches without the `--patch-mode apply` flag. The default must be safe.

---

## Spec patch: single-file output

All spec patches for a single run are applied to the same spec document and written as a single complete YAML file. Not a diff — a complete replacement.

The output is `patches/spec_patched.yaml`. The original spec is unchanged.

This makes it easy to review: `diff openapi.yaml patches/spec_patched.yaml`.

---

## Patch conflict detection

If two drift items produce conflicting patches for the same location (e.g. one says add field X, another says remove field X), flag both as conflicting and write them to `ambiguous.md`. Do not apply either patch.

Conflict detection: before applying any patch, check if any pending patch modifies the same `(target, location)`. If collision detected, move both to ambiguous.

---

## `ambiguous.md` format

```markdown
# Ambiguous Drift Items — Manual Review Required

## 1. DELETE /users/{id} — GHOST_ENDPOINT

**Location:** endpoint
**Code evidence:** Route defined in app/routers/users.py:87
**Spec evidence:** (absent)
**Why ambiguous:** No recent commits to this file. No test coverage. Not referenced in other files.

**Options:**
- If intentional: add this endpoint to the spec
- If forgotten: remove the route from the codebase

---

## 2. POST /users → request_body.schema.metadata — ADDITIVE_DRIFT

**Location:** request_body.schema.metadata
**Code evidence:** metadata: Optional[Dict[str, Any]] in UserCreate (app/schemas.py:34)
**Spec evidence:** (absent)
**Why ambiguous:** Code and spec both modified within 24 hours of each other. No test coverage for this field.

**Options:**
- If intentional: add metadata field to spec's UserCreate schema
- If accidental: remove metadata field from UserCreate model

---
```

---

## `PatchReport` return type

```python
@dataclass
class PatchReport:
    spec_patches_applied: int
    code_patches_applied: int
    ambiguous_count: int
    patch_conflicts: int
    output_dir: str
    files_written: List[str]
    errors: List[str]       # non-fatal errors (backups failed, etc.)
```

---

## Module interface

```python
from drift_agent.patch_generator import PatchGenerator

generator = PatchGenerator(
    spec_path="openapi.yaml",
    project_root="./app",
    output_dir="./patches",
    patch_mode="preview"      # or "apply"
)

report: PatchReport = generator.generate(findings)
```

---

## Tests

Use `tmp_path` pytest fixture for all file I/O. Never write to actual project files in tests.

```python
test_add_field_to_spec()
test_remove_field_from_spec()
test_change_type_in_spec()
test_add_endpoint_to_spec()
test_add_field_to_pydantic_model()          # libcst transformer
test_add_field_preserves_formatting()        # assert whitespace/comments preserved
test_change_type_preserves_rest_of_class()
test_import_added_when_needed()              # datetime import injection
test_spec_patch_roundtrip()                  # parse → patch → parse → compare
test_conflict_detection()
test_ambiguous_md_format()
test_preview_mode_does_not_modify_files()
test_apply_mode_backs_up_first()
test_apply_mode_restores_on_syntax_error()
```

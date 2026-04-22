# code_analyzer

Statically analyzes a FastAPI application source tree using Python's AST and produces a `NormalizedContract` object in the same shape as `spec_parser` output. No LLM involvement. No running server required.

---

## Responsibilities

1. Discover all Python source files in the target directory
2. Build a router graph: identify all `FastAPI()` and `APIRouter()` instances and their prefix chains
3. Extract all route definitions with their full resolved paths
4. Extract parameter schemas from function signatures
5. Resolve Pydantic model definitions to `FieldSchema` trees
6. Extract response schemas from return type annotations and `response_model` arguments
7. Return a `NormalizedContract` with `source="code"`

---

## Input

A directory path containing a FastAPI application. The analyzer recurses into all subdirectories. It processes every `.py` file.

---

## Output

`NormalizedContract` as defined in root `README.md`. Each `EndpointContract` must include `source_file` (relative path from the input directory root) and `source_line` (line number of the route decorator).

---

## Phase 1: Router graph construction

This must complete before route extraction. Routes are meaningless without their resolved paths.

### What to collect

For every `.py` file, do an AST pass collecting:

**FastAPI app instantiations:**
```python
app = FastAPI()
app = FastAPI(title="...", root_path="/v1")   # root_path is a prefix
```

**APIRouter instantiations:**
```python
router = APIRouter()
router = APIRouter(prefix="/users", tags=["users"])
router_v2 = APIRouter(prefix="/v2")
```

**`include_router` calls:**
```python
app.include_router(router)
app.include_router(router, prefix="/api")
app.include_router(users.router, prefix="/users/admin")
```

The effective prefix for a route is the concatenation of all prefixes in the include chain, in order:

```
app.include_router(router, prefix="/api")   →  router prefix = /api + /users = /api/users
router.include_router(sub_router, prefix="/v2")  →  sub_router = /api/users/v2
```

### Router graph data structure

```python
@dataclass
class RouterNode:
    variable_name: str           # Python variable name, e.g. "router", "users_router"
    defined_in: str              # file path
    defined_at_line: int
    instance_prefix: str         # prefix from APIRouter(prefix=...), "" if absent
    include_prefix: str          # prefix from include_router(..., prefix=...), "" if absent
    parent: Optional['RouterNode']  # None for the root app

    @property
    def resolved_prefix(self) -> str:
        # walk parent chain, concatenate all prefixes
        ...
```

Build this graph before processing any routes. If a router is included multiple times with different prefixes, create one `RouterNode` per inclusion.

### Handling cross-file routers

The most common FastAPI pattern:

```python
# users.py
router = APIRouter(prefix="/users")

# main.py
from .users import router as users_router
app.include_router(users_router, prefix="/api")
```

To resolve this, you need import tracking. When you see `from .users import router as users_router`, record the binding `users_router → users.router`. When you see `app.include_router(users_router)`, look up the binding to find the actual `RouterNode`.

This requires a two-pass approach:
1. First pass over all files: collect all router instantiations and `import` statements
2. Second pass: resolve include_router calls using the import bindings

Limit import tracking to project-local imports only (relative imports and absolute imports that resolve within the src directory). Do not attempt to resolve third-party package imports.

---

## Phase 2: Route extraction

For each route decorator found in any file:

```python
@app.get("/items/{item_id}")
@router.post("/", status_code=201)
@users_router.delete("/{user_id}", response_model=UserResponse, status_code=204)
```

Extract:
- `method`: from the decorator name (`get`, `post`, `put`, `patch`, `delete`, `head`, `options`)
- `path_suffix`: the first positional argument to the decorator
- `response_model`: keyword argument if present (an AST Name or Attribute node → resolve to class name)
- `status_code`: keyword argument if present (default: 200 for GET/PUT/PATCH/DELETE, 201 for POST)
- `tags`: keyword argument if present
- `router_node`: the `RouterNode` this decorator belongs to

**Resolved path** = `router_node.resolved_prefix + path_suffix`

Normalize the resolved path:
- Remove double slashes (`//users` → `/users`)
- Ensure leading slash
- No trailing slash (unless path is exactly `/`)

**Endpoint key** = `"{METHOD.upper()} {resolved_path}"` → `"GET /api/users/{user_id}"`

---

## Phase 3: Parameter extraction

For each route function, walk its argument list:

### Path parameters

Any argument whose name appears in the path string as `{name}` is a path parameter. Type annotation determines the schema type. Path parameters are always required.

```python
@app.get("/users/{user_id}/posts/{post_id}")
def get_post(user_id: int, post_id: str):
```

→ `ParameterSchema(name="user_id", location="path", required=True, schema=FieldSchema(type="integer"))`
→ `ParameterSchema(name="post_id", location="path", required=True, schema=FieldSchema(type="string"))`

### Query parameters

Arguments not in the path and not annotated with `Body`, `Header`, or `Cookie`. Default value determines `required`:

```python
def list_users(
    skip: int = 0,                    # optional query param, default 0
    limit: int = Query(default=100),  # optional query param with Query()
    search: str = Query(...),         # required query param (... = no default)
    active: bool = Query(True),       # optional, default True
):
```

`Query(...)` means required. `Query(default=X)` or `= X` means optional with that default.

### Header parameters

```python
x_token: str = Header(...)
x_api_version: Optional[str] = Header(None)
```

Extract name from variable name, converting underscores to hyphens: `x_token` → `X-Token`.

### Body parameters

```python
body: UserCreate = Body(...)
payload: CreateOrderRequest         # type annotation alone, no default → infer as Body
```

If an argument has a Pydantic model type annotation and no `Query`/`Header`/`Cookie` annotation, treat it as the request body. There should be at most one body parameter per route. If multiple Pydantic model arguments exist without explicit `Body()` annotations, emit a warning and use the first one.

### Dependency injection

```python
db: Session = Depends(get_db)
current_user: User = Depends(get_current_user)
```

Arguments annotated with `Depends(...)` are dependency injections. **Skip them entirely** — do not include them as parameters in the contract. They are infrastructure, not API surface.

---

## Phase 4: Pydantic model resolution

When you encounter a Pydantic model class name (from a body parameter or response_model), resolve it to a `FieldSchema`.

### Finding the class definition

1. Check the current file for a class definition matching the name
2. Check import statements: `from .schemas import UserCreate` → look in `schemas.py`
3. If not found after checking current file + imports, emit `UnresolvableModelWarning` and produce `FieldSchema(type="object", description="[unresolved: ClassName]")`

### Extracting fields

For a class like:
```python
class UserCreate(BaseModel):
    name: str
    email: EmailStr
    age: Optional[int] = None
    address: Address
    tags: List[str] = Field(default_factory=list)
    role: Literal["admin", "user"] = "user"
```

Walk the class body looking for annotated assignments. For each:

| Annotation | Canonical type | Notes |
|---|---|---|
| `str` | `string` | |
| `int` | `integer` | |
| `float` | `number` | |
| `bool` | `boolean` | |
| `EmailStr` | `string` | format: `email` |
| `UUID` | `string` | format: `uuid` |
| `datetime` | `string` | format: `date-time` |
| `date` | `string` | format: `date` |
| `Optional[X]` | same as X | `nullable: True` |
| `List[X]` | `array` | `items: resolve(X)` |
| `Dict[str, X]` | `object` | note in description |
| `Literal["a","b"]` | `string` | `enum: ["a","b"]` |
| Another BaseModel | `object` | recurse |
| `Any` | `unknown` | |

**Required vs optional:**
- No default → `required: True`
- `= None` → `required: False`, `nullable: True`
- `= Field(default=...)` or `= Field(default_factory=...)` → `required: False`
- `= Field(...)` (no default) → `required: True`

**Inheritance:**
```python
class UserResponse(UserBase):
    id: int
```
Resolve `UserBase` fields first, then add `UserResponse`'s own fields. Child fields override parent fields with the same name.

**Validators (`@validator`, `@field_validator`):** Ignore them for schema extraction. They may transform values but we cannot statically determine the transformation.

**`model_config` / `Config`:** Check for `alias_generator` or explicit `alias` in `Field(alias=...)`. If aliases are present, use the alias as the field name (since that's what appears in JSON).

---

## Phase 5: Response schema extraction

Three resolution strategies, applied in order of reliability:

### Strategy A: `response_model` decorator argument (highest confidence)

```python
@app.get("/users/{id}", response_model=UserResponse)
```

Resolve `UserResponse` via Phase 4. Mark as `confidence: "explicit"`.

### Strategy B: Return type annotation

```python
def get_user(user_id: int) -> UserResponse:
```

Resolve `UserResponse` via Phase 4. Mark as `confidence: "annotated"`.

```python
def get_user() -> dict:        # too vague, treat as unresolvable
def get_user() -> JSONResponse: # framework response type, treat as unresolvable
```

Only resolve if the return type is a concrete Pydantic model or a generic like `List[UserResponse]`.

### Strategy C: Return statement analysis (lowest confidence, best effort)

Only attempt if strategies A and B both fail.

Walk the function body looking for `return` statements. For each return value:

```python
return {"id": user.id, "name": user.name, "email": user.email}
```

If the return is a dict literal, extract the keys as field names. You cannot reliably infer types from `user.id` etc. without full dataflow analysis. Set all field types to `"unknown"` and mark as `confidence: "inferred"`.

If multiple return statements exist and they return different shapes, take the union of all keys.

If no return statement found, or all returns are non-dict (e.g. `return user` where `user` is a non-model variable), emit `UnresolvableResponseWarning` and set response schema to `None`.

### Response for non-200 status codes

FastAPI automatically generates 422 Unprocessable Entity responses for validation errors. Include this in the contract:

```python
ResponseSchema(
    status_code="422",
    content_type="application/json",
    schema=FieldSchema(type="object", description="[FastAPI validation error response]")
)
```

---

## Unresolvable cases

The following patterns cannot be statically analyzed. Log a warning with file + line number and skip gracefully:

| Pattern | Reason |
|---|---|
| Dynamic path construction: `path = f"/{resource_type}"` | Cannot determine path at parse time |
| `response_model` is a variable: `@app.get("/", response_model=get_model())` | Cannot evaluate function call |
| Routes registered programmatically: `app.add_api_route(path, handler)` | Harder to detect — attempt AST match but flag |
| Heavily nested class-based view patterns | Out of scope for V1 |
| `include_router` with a variable prefix: `app.include_router(r, prefix=get_prefix())` | Cannot evaluate function call |

The contract should still include these endpoints if the route decorator is detectable, but with unresolvable fields set to `None` or the sentinel schema.

---

## Module interface

```python
from drift_agent.code_analyzer import analyze_codebase

contract: NormalizedContract = analyze_codebase("./app")
# raises CodeAnalysisError for unrecoverable failures (e.g. syntax errors in source)
# emits warnings via Python logging for recoverable issues
```

---

## Tests

Test fixtures in `tests/fixtures/apps/`:

```
apps/
  simple_app/           # single file, basic CRUD, all patterns resolved cleanly
  multi_file_app/       # main.py + routers/, cross-file router inclusion
  nested_routers/       # router includes another router
  pydantic_v1_app/      # uses Pydantic v1 syntax (BaseModel, validator)
  pydantic_v2_app/      # uses Pydantic v2 syntax (model_validator, field_validator)
  raw_dict_returns/     # strategy C response extraction
  unresolvable_app/     # dynamic routes, unresolvable models — tests warning emission
  prefix_chains/        # complex prefix resolution
```

Every test asserts on the exact `NormalizedContract` structure. Test warning emission using `pytest.warns` or log capture.

# context_tools

A collection of deterministic tools that the agent calls to gather evidence for source-of-truth inference. Each tool takes structured input and returns structured output. No LLM involvement inside these tools — they are pure data retrieval.

---

## Tool registry

All tools are registered in `tools.py` and exposed to the agent via a tool dispatch function:

```python
def call_tool(name: str, args: dict) -> dict:
    ...
```

The agent calls tools by name with a JSON-serializable args dict. Every tool returns a JSON-serializable dict. Errors are returned as `{"error": "message"}` rather than raised — the agent must handle tool failures gracefully.

---

## Tool specifications

---

### `git_log`

Returns the recent commit history for a specific file.

**Input:**
```json
{
  "filepath": "app/routers/users.py",
  "n": 5
}
```

`filepath`: path relative to the project root (same root passed to `drift-check --src`).
`n`: number of commits to return. Default 5. Max 20.

**Output:**
```json
{
  "filepath": "app/routers/users.py",
  "commits": [
    {
      "hash": "a3f92c1",
      "timestamp": "2024-11-14T09:23:11Z",
      "author": "vidit@example.com",
      "message": "add created_at field to user response",
      "files_changed": ["app/routers/users.py", "app/schemas.py"]
    },
    ...
  ],
  "error": null
}
```

If the file is not tracked by git: `{"error": "file not tracked"}`.
If the directory is not a git repo: `{"error": "not a git repository"}`.
If no commits exist for the file: `{"commits": [], "error": null}`.

**Implementation:** Use `gitpython`. Do not shell out to `git` directly.

```python
import git

repo = git.Repo(project_root, search_parent_directories=True)
commits = list(repo.iter_commits(paths=filepath, max_count=n))
```

---

### `git_log_spec`

Same as `git_log` but for the spec file.

**Input:**
```json
{
  "n": 5
}
```

The spec file path is known from the initial `drift-check` invocation. No need to pass it.

**Output:** Same structure as `git_log`.

---

### `get_commit_message`

Returns the full commit message and diff stat for a specific commit hash.

**Input:**
```json
{
  "hash": "a3f92c1"
}
```

**Output:**
```json
{
  "hash": "a3f92c1a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e",
  "timestamp": "2024-11-14T09:23:11Z",
  "author": "vidit@example.com",
  "message": "add created_at field to user response\n\nAdded ISO 8601 timestamp to UserResponse schema. Clients should use this for display.",
  "diff_stat": {
    "files_changed": 2,
    "insertions": 15,
    "deletions": 3
  },
  "error": null
}
```

---

### `get_test_coverage`

Returns test coverage information for a specific endpoint or source file.

**Input:**
```json
{
  "endpoint": "POST /users",
  "source_file": "app/routers/users.py"
}
```

Either `endpoint` or `source_file` may be omitted. If both provided, both are used to narrow the result.

**Output:**
```json
{
  "has_coverage": true,
  "coverage_percent": 87.5,
  "covered_lines": [45, 46, 47, 51, 52],
  "uncovered_lines": [53, 54],
  "test_files": ["tests/test_users.py"],
  "error": null
}
```

If no coverage data exists: `{"has_coverage": false, "error": null}`.
If coverage tooling is not installed or no `.coverage` file exists: `{"has_coverage": false, "error": "no coverage data found"}`.

**Implementation:**

Check for a `.coverage` file in the project root. If found, use the `coverage` Python API:

```python
import coverage as cov_module

cov = cov_module.Coverage(data_file=".coverage")
cov.load()
analysis = cov.analysis2(source_file)
```

If no `.coverage` file, check for `coverage.xml` (common in CI). Parse it as a fallback.

If neither exists, return the no-coverage response without error — missing coverage data is not an error.

---

### `search_codebase`

Searches the codebase for a pattern. Used to determine if an undocumented endpoint or field is referenced internally.

**Input:**
```json
{
  "pattern": "created_at",
  "file_glob": "**/*.py",
  "context_lines": 2
}
```

`pattern`: a literal string or simple glob pattern. Not a regex (to keep it safe and predictable).
`file_glob`: glob pattern for files to search. Default `"**/*.py"`.
`context_lines`: lines of context around each match. Default 2.

**Output:**
```json
{
  "matches": [
    {
      "file": "app/schemas.py",
      "line": 23,
      "match": "    created_at: datetime",
      "context": ["class UserResponse(BaseModel):", "    id: int", "    name: str", "    created_at: datetime", "    email: str"]
    },
    {
      "file": "tests/test_users.py",
      "line": 47,
      "match": "    assert response.json()[\"created_at\"] is not None",
      "context": [...]
    }
  ],
  "total_matches": 2,
  "error": null
}
```

Limit to 50 matches max. If more exist, include `"truncated": true` in the response.

**Implementation:** Use Python's `pathlib` + `fnmatch` for file globbing. Use string `.find()` for matching — no regex, no subprocess.

---

### `read_file_slice`

Returns a slice of a source file with line numbers. Used when the agent needs more context than the AST extraction provided.

**Input:**
```json
{
  "filepath": "app/routers/users.py",
  "start_line": 40,
  "end_line": 60
}
```

Lines are 1-indexed, inclusive. `end_line` defaults to `start_line + 30` if omitted.

**Output:**
```json
{
  "filepath": "app/routers/users.py",
  "start_line": 40,
  "end_line": 60,
  "content": "    @router.post(\"/\", response_model=UserResponse)\n    def create_user(body: UserCreate, db: Session = Depends(get_db)):\n...",
  "error": null
}
```

Max slice size: 100 lines. If `end_line - start_line > 100`, truncate to 100 lines and include `"truncated": true`.

---

### `list_endpoints_with_tests`

Returns a mapping of which endpoints have corresponding test functions. Used for bulk assessment when the agent processes multiple drift items.

**Input:**
```json
{}
```

No input required.

**Output:**
```json
{
  "endpoint_test_map": {
    "GET /users": ["tests/test_users.py::test_list_users", "tests/test_users.py::test_list_users_empty"],
    "POST /users": ["tests/test_users.py::test_create_user"],
    "DELETE /users/{id}": []
  },
  "error": null
}
```

**Implementation:** Walk the `tests/` directory (or any directory matching `test_*.py` or `*_test.py`). Look for test function names that contain endpoint path fragments. This is heuristic — a test named `test_create_user` is likely testing `POST /users`. Use simple substring matching on both path components and HTTP method names.

Not all endpoints will be matched. Missing entries in the map mean no tests were detected, not that no tests exist.

---

## Tool context object

All tools need access to the project root and spec file path. These are injected at initialization, not passed per call:

```python
from drift_agent.context_tools import ContextToolkit

toolkit = ContextToolkit(
    project_root="./app",
    spec_path="openapi.yaml"
)
```

The toolkit is passed to the agent at construction. The agent calls `toolkit.call_tool(name, args)`.

---

## Error contract

Every tool must return a dict with an `"error"` key. `null` means success. A non-null string is an error message.

The agent must never crash on a tool error. When a tool returns an error, the agent should:
1. Note the error in its reasoning
2. Assign lower confidence to any finding that relied on that tool
3. Continue to the next drift item

---

## Tests

Each tool has unit tests. Tests should not require a real git repo, real coverage files, or a real filesystem — use `tmp_path` fixtures and mock data.

```python
test_git_log_normal()
test_git_log_untracked_file()
test_git_log_not_a_repo()
test_search_codebase_finds_matches()
test_search_codebase_no_matches()
test_read_file_slice_normal()
test_read_file_slice_truncation()
test_get_test_coverage_no_data()
test_coverage_with_coverage_file()   # requires a real .coverage fixture
```

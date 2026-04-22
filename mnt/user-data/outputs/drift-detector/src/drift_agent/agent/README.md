# agent

The orchestration core. A local LLM (Qwen2.5-Coder-7B via Ollama) that receives drift items from the diff engine, calls context tools to gather evidence, reasons about source-of-truth, and produces `AgentFinding` objects with patches and natural language justifications.

---

## Model

**Model:** `qwen2.5-coder:7b`
**Runtime:** Ollama (must be running locally on port 11434)
**Context window:** 32k tokens — sufficient for all tool outputs + system prompt + reasoning
**Interface:** Ollama Python client (`ollama` package)

Check for model availability at startup:
```python
import ollama
try:
    ollama.show("qwen2.5-coder:7b")
except ollama.ResponseError:
    raise ModelNotAvailableError(
        "qwen2.5-coder:7b not found. Run: ollama pull qwen2.5-coder:7b"
    )
```

---

## Agent loop

The agent processes drift items **one at a time**. For each `DriftItem`:

```
1. Initial assessment
   → Based on category and severity, decide if context tools are needed
   → Some categories are clear without tools (see Fast-path decisions)

2. Tool call phase (0 or more iterations)
   → Call tools to gather evidence
   → Decide if enough evidence exists to make a confident determination
   → Maximum 5 tool calls per drift item (hard limit)

3. Finding generation
   → Determine source_of_truth: CODE | SPEC | AMBIGUOUS
   → Assign confidence: high | medium | low
   → Write reasoning string citing specific evidence
   → Generate patch if source_of_truth is not AMBIGUOUS
```

---

## Fast-path decisions (no tool calls)

The agent should recognize these patterns immediately without calling any tools:

| Condition | Decision | Confidence |
|---|---|---|
| `DESTRUCTIVE_DRIFT` — spec documents a field, code doesn't return it | `SPEC` is probably wrong OR code has a bug. Default: flag as `AMBIGUOUS` unless other signals exist | medium |
| `TYPE_DRIFT` where code type is a strict subtype of spec type (e.g. `integer` code, `number` spec) | `CODE` is fine, spec is overly broad. Patch spec. | high |
| `NULLABILITY_DRIFT` where code is non-nullable and spec is nullable | `CODE` is stricter. Patch spec. | medium |
| `ADDITIVE_DRIFT` on a field named `*_at` (e.g. `created_at`, `updated_at`) | Likely intentional timestamp field. Call git_log to confirm. | — |
| `STATUS_CODE_DRIFT` category `info` | Skip — don't call tools, don't generate patch | — |

---

## Decision heuristics (with tool calls)

These are the signals the agent uses. The agent must explicitly cite which signals it used in its `reasoning` string.

### Recency signal

Call `git_log` on the relevant source file and `git_log_spec`. Compare timestamps of the most recent commit to each.

- Code changed more recently than spec → lean toward `CODE` as source of truth
- Spec changed more recently than code → lean toward `SPEC` as source of truth
- Both changed within 48h of each other → recency is inconclusive, do not use as primary signal
- Neither changed in >30 days → stale drift, likely not intentional either way

### Commit message signal

Call `get_commit_message` for the most recent relevant commit. Scan the message for:
- Keywords suggesting intentional addition: `add`, `introduce`, `implement`, `feature`, `new`
- Keywords suggesting bug fix: `fix`, `bugfix`, `hotfix`, `correct`, `resolve`
- Keywords suggesting removal: `remove`, `delete`, `deprecate`, `drop`
- Spec-related keywords: `openapi`, `spec`, `schema`, `contract`, `docs`, `documentation`

If commit message explicitly describes the drift (e.g. "add created_at to response") → strong signal for that side being intentional.

### Test coverage signal

Call `get_test_coverage` for the relevant endpoint.

- Coverage > 80% AND test explicitly asserts the drifted field → strong signal for `CODE` being intentional
- No coverage → weaker signal, rely on other evidence
- Coverage exists but doesn't test the specific field → neutral

### Internal reference signal

For `GHOST_ENDPOINT` drift items: call `search_codebase` for the path string.

- Found in test files → probably intentional
- Found only in the route definition itself → no external usage, possibly forgotten
- Not found anywhere else → likely a candidate for documentation OR removal

### Additive drift signal

For `ADDITIVE_DRIFT`: call `search_codebase` for the field name.

- Found in test files with assertions → field is intentional, code is source of truth
- Not tested → could be accidental leakage

---

## Confidence assignment

```
high    → two or more signals agree, no conflicting signals
medium  → one strong signal, no conflicting signals
         OR two signals with minor conflict
low     → signals conflict, or only weak signals available
AMBIGUOUS → confidence would be "low" AND patch generation is risky
```

Set `source_of_truth = "AMBIGUOUS"` when:
- Signals actively conflict (e.g. code changed recently but spec message explicitly updated the contract)
- Only `low` confidence is achievable
- The drift involves both additive AND destructive changes in the same schema (reshaping)

---

## Reasoning string format

The reasoning string must be a single paragraph of natural language. It must:
1. State the drift category and location
2. Name each piece of evidence used
3. State why it points to the conclusion
4. Acknowledge any conflicting signals

```
"ADDITIVE_DRIFT on response.201.schema.created_at. Code added this field in commit a3f92c1 
('add created_at to user response', 3 days ago) — commit message confirms intentional addition. 
Test coverage at tests/test_users.py:47 asserts the field is present. Spec was last modified 
3 weeks ago with no mention of this field. All signals point to CODE as source of truth. 
Patch: add created_at (string/date-time) to spec's UserResponse schema."
```

---

## Tool call budget

Max 5 tool calls per drift item. Budget allocation guidance (not enforced mechanically — the agent decides):

| Drift category | Expected tool calls |
|---|---|
| Fast-path (no tools) | 0 |
| Simple recency check | 2 (git_log + git_log_spec) |
| With commit message | 3 (git_log + git_log_spec + get_commit_message) |
| Full investigation | 4-5 (above + coverage + search) |

If the budget is exhausted and source-of-truth is still unclear → set `AMBIGUOUS`.

---

## System prompt

The system prompt is defined in `prompts.py`. It must include:

1. Role definition — the agent is a static analysis tool, not a general assistant
2. The complete tool schema (JSON) for all available tools
3. The output schema — exact JSON format the agent must produce per drift item
4. Decision heuristics (summary of the above)
5. Explicit instruction: "Call tools only when needed. Do not call tools for fast-path cases. Stop calling tools when you have enough evidence. Always produce a finding — never leave a drift item unresolved."

The system prompt must not include examples of actual codebases. Keep it schema-focused.

### Tool schema format (for Ollama function calling)

```python
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": "Get recent git commit history for a source file",
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {"type": "string", "description": "Path relative to project root"},
                    "n": {"type": "integer", "description": "Number of commits, default 5, max 20"}
                },
                "required": ["filepath"]
            }
        }
    },
    # ... one entry per tool
]
```

---

## Agent output schema

For each drift item, the agent must produce exactly this JSON:

```json
{
  "drift_item_id": "a3f92c1b4d5e",
  "source_of_truth": "CODE",
  "confidence": "high",
  "reasoning": "...",
  "tools_called": ["git_log", "git_log_spec", "get_commit_message"],
  "patch": {
    "target": "spec",
    "patch_type": "add_field",
    "location": "components/schemas/UserResponse/properties",
    "content": "created_at:\n  type: string\n  format: date-time"
  }
}
```

If `source_of_truth == "AMBIGUOUS"`, `patch` must be `null`.

If the agent produces malformed JSON, retry once with an explicit correction prompt. If the second attempt also fails, emit `AgentFailure` for that item and continue.

---

## Ollama integration

```python
import ollama

response = ollama.chat(
    model="qwen2.5-coder:7b",
    messages=conversation_history,
    tools=TOOLS,
    options={"temperature": 0.1}   # low temperature for deterministic reasoning
)
```

Use `temperature=0.1`. This is a reasoning task, not a creative one. Higher temperature introduces noise in JSON output and heuristic application.

**Multi-turn conversation per drift item:**

The agent conversation history for a single drift item looks like:

```
[system]: <system prompt>
[user]: <drift item JSON + endpoint context>
[assistant]: <tool call: git_log>
[tool]: <git_log result>
[assistant]: <tool call: git_log_spec>
[tool]: <git_log_spec result>
[assistant]: <final JSON finding>
```

Each drift item starts a fresh conversation (fresh `messages` list). Do not carry context between drift items.

---

## Parallelism

Do not process drift items in parallel in V1. The Ollama server handles one request at a time efficiently; concurrent requests queue anyway. Sequential processing simplifies error handling and produces a more predictable log output.

---

## Module interface

```python
from drift_agent.agent import DriftAgent

agent = DriftAgent(
    toolkit=context_toolkit,
    model="qwen2.5-coder:7b",
    ollama_host="http://localhost:11434"
)

findings: List[AgentFinding] = agent.analyze(drift_items)
```

`analyze` is synchronous. It processes all items and returns when complete.

If Ollama is not running, raise `OllamaConnectionError` immediately with instructions.

---

## Tests

Testing the agent in unit tests is hard (requires a running Ollama instance). Instead:

**Unit tests:** Mock the Ollama client. Test that:
- Fast-path items don't call any tools
- Tool call budget is respected (≤5 calls)
- Malformed JSON triggers retry logic
- `AgentFailure` is emitted correctly

**Integration tests (marked `@pytest.mark.integration`):** Require Ollama. Run against a small set of synthetic drift items with known ground-truth answers. Assert that the agent reaches the correct `source_of_truth` determination at least 80% of the time.

```python
@pytest.mark.integration
def test_agent_additive_drift_with_commit_evidence():
    # set up a real git repo fixture with a commit that adds a field
    # run the agent
    # assert source_of_truth == "CODE" with confidence in ("high", "medium")
    ...
```

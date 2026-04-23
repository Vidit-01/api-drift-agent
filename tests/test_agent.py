from drift_agent.agent.core import DriftAgent
from drift_agent.context_tools import ContextToolkit
from drift_agent.types import DriftCategory, DriftItem


class FakeClient:
    def show(self, model):
        return {"name": model}

    def chat(self, **kwargs):
        return {"message": {"content": "{\"drift_item_id\": \"abc123\", \"source_of_truth\": \"CODE\", \"confidence\": \"high\", \"reasoning\": \"Looks intentional.\", \"tools_called\": [], \"patch\": {\"target\": \"spec\", \"patch_type\": \"add_field\", \"location\": \"components/schemas/UserResponse/properties\", \"content\": \"created_at:\\n  type: string\"}}"}}


class BadJsonClient:
    def show(self, model):
        return {"name": model}

    def chat(self, **kwargs):
        return {"message": {"content": "I cannot produce JSON for this item."}}


def test_agent_fast_path(monkeypatch, tmp_path):
    monkeypatch.setattr("drift_agent.agent.core.ollama.Client", lambda host: FakeClient())
    toolkit = ContextToolkit(project_root=tmp_path, spec_path=tmp_path / "openapi.yaml")
    agent = DriftAgent(toolkit=toolkit)
    finding = agent.analyze(
        [
            DriftItem(
                id="1",
                endpoint="GET /users",
                category=DriftCategory.STATUS_CODE_DRIFT,
                location="response.204",
                detail="Extra code status",
                spec_evidence=None,
                code_evidence="204",
                severity="info",
            )
        ]
    )[0]

    assert finding.source_of_truth == "AMBIGUOUS"
    assert finding.patch is None


def test_agent_extracts_json_from_fenced_response():
    agent = DriftAgent.__new__(DriftAgent)

    payload = agent._parse_json_object(
        """
        Here is the result:
        ```json
        {"source_of_truth": "CODE", "confidence": "high", "reasoning": "ok", "patch": null}
        ```
        """
    )

    assert payload["source_of_truth"] == "CODE"


def test_agent_parses_yaml_like_object_response():
    agent = DriftAgent.__new__(DriftAgent)

    payload = agent._parse_json_object(
        """
        source_of_truth: CODE
        confidence: high
        reasoning: ok
        patch:
          target: spec
          patch_type: change_type
          location: paths//api/orders/get/parameters/0/schema/type
          content: integer
        """
    )

    assert payload["patch"]["content"] == "integer"


def test_agent_marks_malformed_model_response_ambiguous(monkeypatch, tmp_path):
    monkeypatch.setattr("drift_agent.agent.core.ollama.Client", lambda host: BadJsonClient())
    toolkit = ContextToolkit(project_root=tmp_path, spec_path=tmp_path / "openapi.yaml")
    agent = DriftAgent(toolkit=toolkit)

    finding = agent.analyze(
        [
            DriftItem(
                id="1",
                endpoint="GET /users",
                category=DriftCategory.ADDITIVE_DRIFT,
                location="response.200.schema.name",
                detail="Missing field",
                spec_evidence=None,
                code_evidence="string",
                severity="warning",
            )
        ]
    )[0]

    assert finding.source_of_truth == "AMBIGUOUS"
    assert finding.patch is None


from drift_agent.agent.core import DriftAgent
from drift_agent.context_tools import ContextToolkit
from drift_agent.types import DriftCategory, DriftItem


class FakeClient:
    def show(self, model):
        return {"name": model}

    def chat(self, **kwargs):
        return {"message": {"content": "{\"drift_item_id\": \"abc123\", \"source_of_truth\": \"CODE\", \"confidence\": \"high\", \"reasoning\": \"Looks intentional.\", \"tools_called\": [], \"patch\": {\"target\": \"spec\", \"patch_type\": \"add_field\", \"location\": \"components/schemas/UserResponse/properties\", \"content\": \"created_at:\\n  type: string\"}}"}}


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


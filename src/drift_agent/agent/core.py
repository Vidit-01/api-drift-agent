from __future__ import annotations

import json
import textwrap
from dataclasses import asdict
from typing import Any, Callable

import ollama
import yaml

from drift_agent.agent.prompts import SYSTEM_PROMPT, TOOLS
from drift_agent.agent.tools import call_tool
from drift_agent.context_tools import ContextToolkit
from drift_agent.errors import AgentFailure, ModelNotAvailableError, OllamaConnectionError
from drift_agent.types import AgentFinding, DriftCategory, DriftItem, PatchSpec


class DriftAgent:
    def __init__(
        self,
        toolkit: ContextToolkit,
        model: str = "qwen2.5-coder:7b",
        ollama_host: str = "http://localhost:11434",
    ):
        self.toolkit = toolkit
        self.model = model
        self.client = ollama.Client(host=ollama_host)
        self._check_model()

    def analyze(self, drift_items: list[DriftItem], on_finding: Callable[[AgentFinding, int, int], None] | None = None) -> list[AgentFinding]:
        findings: list[AgentFinding] = []
        total = len(drift_items)
        for index, item in enumerate(drift_items, start=1):
            finding = self._analyze_item(item)
            findings.append(finding)
            if on_finding:
                on_finding(finding, index, total)
        return findings

    def _check_model(self) -> None:
        try:
            self.client.show(self.model)
        except ollama.ResponseError as exc:
            raise ModelNotAvailableError(f"{self.model} not found. Run: ollama pull {self.model}") from exc
        except Exception as exc:
            raise OllamaConnectionError("Could not connect to Ollama. Ensure it is running locally.") from exc

    def _analyze_item(self, drift_item: DriftItem) -> AgentFinding:
        fast_path = self._fast_path(drift_item)
        if fast_path is not None:
            return fast_path
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "drift_item": drift_item.to_dict(),
                        "guidance": "Investigate with tools only if necessary and return the required JSON object.",
                    }
                ),
            },
        ]
        tools_called: list[str] = []
        for _ in range(6):
            response = self.client.chat(
                model=self.model,
                messages=messages,
                tools=TOOLS,
                options={"temperature": 0.1},
            )
            message = self._response_message(response)
            tool_calls = message.get("tool_calls") or []
            if tool_calls:
                if len(tools_called) >= 5:
                    break
                messages.append({"role": "assistant", "content": message.get("content", ""), "tool_calls": tool_calls})
                for tool_call in tool_calls:
                    function_data = tool_call.get("function", {})
                    tool_name = function_data.get("name")
                    if tool_name is None:
                        continue
                    arguments = function_data.get("arguments") or {}
                    if isinstance(arguments, str):
                        arguments = json.loads(arguments or "{}")
                    tools_called.append(tool_name)
                    result = call_tool(self.toolkit, tool_name, arguments)
                    messages.append({"role": "tool", "name": tool_name, "content": json.dumps(result)})
                continue
            try:
                payload = self._parse_json_with_retry(messages, message.get("content", ""))
            except AgentFailure as exc:
                return self._ambiguous_finding(drift_item, tools_called, str(exc))
            return self._finding_from_payload(drift_item, payload, tools_called)
        return AgentFinding(
            drift_item=drift_item,
            source_of_truth="AMBIGUOUS",
            confidence="low",
            reasoning=f"{drift_item.category.value} at {drift_item.location}. Tool-call budget was exhausted before a reliable conclusion was reached.",
            evidence=[],
            patch=None,
        )

    def _ambiguous_finding(self, drift_item: DriftItem, tools_called: list[str], reason: str) -> AgentFinding:
        return AgentFinding(
            drift_item=drift_item,
            source_of_truth="AMBIGUOUS",
            confidence="low",
            reasoning=f"{drift_item.category.value} at {drift_item.location}. {reason}",
            evidence=[{"tools_called": tools_called}],
            patch=None,
        )

    def _parse_json_with_retry(self, messages: list[dict[str, Any]], content: str) -> dict[str, Any]:
        try:
            return self._parse_json_object(content)
        except json.JSONDecodeError:
            messages.extend(
                [
                    {"role": "assistant", "content": content},
                    {"role": "user", "content": "Your previous response was not valid JSON. Return only the required JSON object."},
                ]
            )
            retry = self.client.chat(
                model=self.model,
                messages=messages,
                tools=TOOLS,
                options={"temperature": 0.1},
            )
            retry_content = self._response_message(retry).get("content", "")
            try:
                return self._parse_json_object(retry_content)
            except json.JSONDecodeError as exc:
                raise AgentFailure("Agent returned malformed JSON twice") from exc

    def _parse_json_object(self, content: str) -> dict[str, Any]:
        decoder = json.JSONDecoder()
        stripped = textwrap.dedent(content).strip()
        candidates = [stripped]
        if "```" in stripped:
            candidates.extend(part.strip() for part in stripped.split("```") if part.strip())
            candidates.extend(part.removeprefix("json").strip() for part in stripped.split("```") if part.strip().startswith("json"))
        for candidate in candidates:
            try:
                payload = json.loads(candidate)
                if isinstance(payload, dict):
                    return payload
            except json.JSONDecodeError:
                pass
            for index, char in enumerate(candidate):
                if char != "{":
                    continue
                try:
                    payload, _ = decoder.raw_decode(candidate[index:])
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict):
                    return payload
            try:
                payload = yaml.safe_load(candidate)
            except yaml.YAMLError:
                continue
            if isinstance(payload, dict):
                return payload
        raise json.JSONDecodeError("No JSON object found", content, 0)

    def _finding_from_payload(self, drift_item: DriftItem, payload: dict[str, Any], tools_called: list[str]) -> AgentFinding:
        patch_data = payload.get("patch")
        patch = None
        if patch_data:
            patch = PatchSpec(
                target=patch_data["target"],
                patch_type=patch_data["patch_type"],
                location=patch_data["location"],
                content=patch_data["content"],
            )
        return AgentFinding(
            drift_item=drift_item,
            source_of_truth=payload["source_of_truth"],
            confidence=payload["confidence"],
            reasoning=payload["reasoning"],
            evidence=[{"tools_called": tools_called}],
            patch=patch,
        )

    def _fast_path(self, drift_item: DriftItem) -> AgentFinding | None:
        if drift_item.category == DriftCategory.STATUS_CODE_DRIFT and drift_item.severity == "info":
            return AgentFinding(
                drift_item=drift_item,
                source_of_truth="AMBIGUOUS",
                confidence="low",
                reasoning=f"STATUS_CODE_DRIFT at {drift_item.location}. Extra response codes are informational in this workflow, so no automatic patch is generated.",
                evidence=[],
                patch=None,
            )
        if drift_item.category == DriftCategory.NULLABILITY_DRIFT and drift_item.spec_evidence == "True" and drift_item.code_evidence == "False":
            return AgentFinding(
                drift_item=drift_item,
                source_of_truth="CODE",
                confidence="medium",
                reasoning=f"NULLABILITY_DRIFT at {drift_item.location}. Code is stricter than the spec, which is generally safe, so the spec should be tightened to match code.",
                evidence=[],
                patch=self._default_patch(drift_item, target="spec"),
            )
        if drift_item.category == DriftCategory.TYPE_DRIFT and drift_item.spec_evidence == "number" and drift_item.code_evidence == "integer":
            return AgentFinding(
                drift_item=drift_item,
                source_of_truth="CODE",
                confidence="high",
                reasoning=f"TYPE_DRIFT at {drift_item.location}. Integer is a valid subtype of number, so the code is more specific and the spec should be narrowed.",
                evidence=[],
                patch=self._default_patch(drift_item, target="spec"),
            )
        return None

    def _default_patch(self, drift_item: DriftItem, target: str) -> PatchSpec:
        patch_type = "change_type" if drift_item.category == DriftCategory.TYPE_DRIFT else "change_schema"
        location = drift_item.location.replace(".", "/")
        content = drift_item.code_evidence or drift_item.spec_evidence or drift_item.detail
        return PatchSpec(target=target, patch_type=patch_type, location=location, content=content)

    def _response_message(self, response: Any) -> dict[str, Any]:
        if isinstance(response, dict):
            return response.get("message", {})
        if hasattr(response, "message"):
            message = response.message
            if isinstance(message, dict):
                return message
            return {
                "content": getattr(message, "content", ""),
                "tool_calls": getattr(message, "tool_calls", None),
            }
        return {}

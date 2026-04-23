from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.console import Console
from rich.table import Table

from drift_agent.agent import DriftAgent
from drift_agent.code_analyzer import analyze_codebase
from drift_agent.context_tools import ContextToolkit
from drift_agent.diff_engine import compute_drift
from drift_agent.patch_generator import PatchGenerator
from drift_agent.spec_parser import parse_spec
from drift_agent.types import AgentFinding, DriftItem

app = typer.Typer(help="Detect drift between an OpenAPI contract and a FastAPI codebase.")
console = Console()


@app.command()
def main(
    spec: str | None = typer.Option(None, "--spec"),
    src: str | None = typer.Option(None, "--src"),
    explain: bool = typer.Option(False, "--explain"),
    patch_dir: str | None = typer.Option(None, "--patch-dir"),
    patch_mode: str = typer.Option("preview", "--patch-mode"),
    output_format: str = typer.Option("rich", "--output-format"),
    output_file: str | None = typer.Option(None, "--output-file"),
    exit_code: bool = typer.Option(False, "--exit-code"),
    model: str = typer.Option("qwen2.5-coder:7b", "--model"),
) -> None:
    config = _load_config(Path.cwd() / ".drift-check.yml")
    spec = spec or config.get("spec")
    src = src or config.get("src")
    patch_dir = patch_dir or config.get("patch_dir") or "patches"
    if not spec or not src:
        raise typer.BadParameter("Both --spec and --src are required, either directly or via .drift-check.yml")
    spec_contract = parse_spec(spec)
    code_contract = analyze_codebase(src)
    drift_items = _apply_ignore_rules(compute_drift(spec_contract, code_contract), config.get("ignore", []))
    findings: list[AgentFinding] = []
    patch_report = None
    if explain and drift_items:
        toolkit = ContextToolkit(project_root=src, spec_path=spec)
        findings = DriftAgent(toolkit=toolkit, model=model).analyze(drift_items)
        if patch_dir:
            patch_report = PatchGenerator(spec_path=spec, project_root=src, output_dir=patch_dir, patch_mode=patch_mode).generate(findings)
    payload = _build_output(spec, src, spec_contract, code_contract, drift_items, findings, patch_report)
    _emit_output(payload, output_format=output_format, output_file=output_file)
    if exit_code and payload["summary"]["error"] > 0:
        raise typer.Exit(code=1)


def _load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _apply_ignore_rules(items: list[DriftItem], ignore_rules: list[dict[str, Any]]) -> list[DriftItem]:
    filtered: list[DriftItem] = []
    for item in items:
        ignored = False
        for rule in ignore_rules:
            endpoint_rule = rule.get("endpoint")
            category_rule = rule.get("category")
            if endpoint_rule and not fnmatch(item.endpoint, endpoint_rule):
                continue
            if category_rule and item.category.value != category_rule:
                continue
            ignored = True
            break
        if not ignored:
            filtered.append(item)
    return filtered


def _build_output(
    spec: str,
    src: str,
    spec_contract,
    code_contract,
    drift_items: list[DriftItem],
    findings: list[AgentFinding],
    patch_report,
) -> dict[str, Any]:
    summary = Counter(item.severity for item in drift_items)
    payload = {
        "run_id": uuid.uuid4().hex[:8],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "spec_path": spec,
        "src_path": src,
        "spec_endpoints": len(spec_contract.endpoints),
        "code_endpoints": len(code_contract.endpoints),
        "items": [item.to_dict() for item in drift_items],
        "summary": {
            "error": summary.get("error", 0),
            "warning": summary.get("warning", 0),
            "info": summary.get("info", 0),
            "total": len(drift_items),
        },
    }
    if findings:
        payload["findings"] = [
            {
                "drift_item_id": finding.drift_item.id,
                "source_of_truth": finding.source_of_truth,
                "confidence": finding.confidence,
                "reasoning": finding.reasoning,
                "patch": finding.patch.to_dict() if finding.patch else None,
            }
            for finding in findings
        ]
    if patch_report:
        payload["patch_report"] = patch_report.to_dict()
    return payload


def _emit_output(payload: dict[str, Any], output_format: str, output_file: str | None) -> None:
    if output_format == "json":
        text = json.dumps(payload, indent=2)
        console.print(text)
    else:
        table = Table(title=f"DRIFT REPORT  {payload['spec_path']} <-> {payload['src_path']}")
        table.add_column("Severity")
        table.add_column("Endpoint")
        table.add_column("Location")
        table.add_column("Detail")
        for item in payload["items"]:
            table.add_row(item["severity"].upper(), item["endpoint"], item["location"], item["detail"])
        console.print(table)
        console.print(
            f"{payload['summary']['total']} drift items | "
            f"{payload['summary']['error']} errors | "
            f"{payload['summary']['warning']} warnings | "
            f"{payload['summary']['info']} info"
        )
    if output_file:
        Path(output_file).write_text(json.dumps(payload, indent=2), encoding="utf-8")


if __name__ == "__main__":
    app()

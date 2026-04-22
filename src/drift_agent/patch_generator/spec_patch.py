from __future__ import annotations

import io
from pathlib import Path

from ruamel.yaml import YAML

from drift_agent.types import PatchSpec


def apply_spec_patches(spec_path: Path, patches: list[PatchSpec]) -> str:
    yaml = YAML()
    yaml.preserve_quotes = True
    document = yaml.load(spec_path.read_text(encoding="utf-8"))
    for patch in patches:
        location_parts = [part for part in patch.location.split("/") if part]
        parent = document
        for part in location_parts[:-1]:
            parent = parent[part]
        key = location_parts[-1] if location_parts else None
        if patch.patch_type == "add_field":
            content = yaml.load(io.StringIO(patch.content))
            parent[key].update(content) if key and key in parent else parent.update(content)
        elif patch.patch_type == "remove_field":
            if key:
                parent.pop(key, None)
        else:
            content = yaml.load(io.StringIO(patch.content))
            if key:
                parent[key] = content
    buffer = io.StringIO()
    yaml.dump(document, buffer)
    return buffer.getvalue()


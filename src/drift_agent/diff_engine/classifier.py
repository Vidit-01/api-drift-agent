from __future__ import annotations

import hashlib

from drift_agent.types import DriftCategory


def make_drift_id(endpoint: str, location: str, category: DriftCategory) -> str:
    raw = f"{endpoint}|{location}|{category.value}"
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


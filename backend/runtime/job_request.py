"""Serialize generation requests for isolated workers."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any


def _json_serializable(value: Any) -> Any:
    """Recursively convert request values to JSON-native types."""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return _json_serializable(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return _json_serializable(asdict(value))
    if hasattr(value, "model_dump"):
        return _json_serializable(value.model_dump(exclude_none=True))
    if isinstance(value, dict):
        return {str(key): _json_serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_serializable(item) for item in value]
    return value


def serialize_generation_request(request: Any) -> dict[str, Any]:
    """Return a JSON-serializable GenerationRequest payload."""
    if is_dataclass(request):
        raw = asdict(request)
    else:
        raw = dict(vars(request))
    return _json_serializable(raw)

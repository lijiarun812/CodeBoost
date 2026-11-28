"""Provider-neutral candidate generation contract; no API client or credentials.

A real adapter closes over its own client/configuration and implements
``generate(prompt, current_code, round_index) -> CandidateResponse``.
It must copy the provider's *returned* model/version and effective generation
parameters into the response. Credentials never belong in these fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class CandidateResponse:
    code: str | None
    model: str | None
    actual_model_version: str | None
    parameters: Mapping[str, Any] = field(default_factory=dict)
    failure_reason: str | None = None


class CandidateGenerator(Protocol):
    def __call__(self, prompt: str, current_code: str,
                 round_index: int) -> CandidateResponse | str: ...


def generation_record(value: CandidateResponse | str) -> dict:
    """Legacy code-only mocks stay usable but cannot prove a model version."""
    if isinstance(value, str):
        return {"status": "legacy_code_only", "candidate_code": value,
                "model": None, "actual_model_version": None,
                "parameters": {}, "failure_reason": None}
    if not isinstance(value, CandidateResponse):
        raise TypeError("generator must return CandidateResponse or code string")
    parameters = dict(value.parameters)
    if value.failure_reason:
        status = "api_failed"
    elif not isinstance(value.model, str) or not value.model.strip():
        status = "invalid_response"
    elif not isinstance(value.actual_model_version, str) or not value.actual_model_version.strip():
        status = "invalid_response"
    elif not isinstance(value.code, str) or not value.code.strip():
        status = "invalid_response"
    elif ("temperature" in parameters and
          (isinstance(parameters["temperature"], bool) or
           not isinstance(parameters["temperature"], (int, float)) or
           not math.isfinite(parameters["temperature"]))):
        status = "invalid_response"
    elif ("seed" in parameters and
          (isinstance(parameters["seed"], bool) or
           not isinstance(parameters["seed"], int))):
        status = "invalid_response"
    else:
        status = "ok"
    reason = (value.failure_reason or
              ("invalid_model_version_code_or_parameters" if status == "invalid_response" else None))
    return {"status": status, "candidate_code": value.code,
            "model": value.model, "actual_model_version": value.actual_model_version,
            "parameters": parameters, "failure_reason": reason}

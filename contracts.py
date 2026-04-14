from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


Mode = Literal["plan_only", "execute"]
Workflow = Literal["auto", "sequential", "parallel"]
GateDecision = Literal["pass", "fail", "escalate", "not_run"]


class RequestValidationError(ValueError):
    """Raised when a tool payload cannot be normalized safely."""


@dataclass(slots=True)
class RequestContext:
    goal: str | None = None
    files: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RequestConfig:
    mode: Mode = "plan_only"
    workflow: Workflow = "auto"
    coverage_target: float = 0.8
    model: str | None = None  # Default model
    provider: str = "codex"  # Default provider: "codex" or "claude"
    models: dict[str, str] = field(default_factory=dict)  # Per-agent models
    providers: dict[str, str] = field(default_factory=dict)  # Per-agent providers


@dataclass(slots=True)
class TeamRequest:
    task: str
    context: RequestContext = field(default_factory=RequestContext)
    config: RequestConfig = field(default_factory=RequestConfig)

    def __init__(
        self,
        task: str,
        context: RequestContext | dict[str, Any] | None = None,
        config: RequestConfig | dict[str, Any] | None = None,
    ) -> None:
        self.task = _normalize_task(task)
        self.context = _normalize_context(context or {})
        self.config = _normalize_config(config or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "context": asdict(self.context),
            "config": asdict(self.config),
        }


def normalize_request(payload: dict[str, Any]) -> TeamRequest:
    if not isinstance(payload, dict):
        raise RequestValidationError("Request payload must be a JSON object.")

    if "task" not in payload:
        raise RequestValidationError("Missing required field: task.")

    return TeamRequest(
        task=payload["task"],
        context=payload.get("context"),
        config=payload.get("config"),
    )


def _normalize_task(value: Any) -> str:
    if not isinstance(value, str):
        raise RequestValidationError("task must be a string.")

    task = value.strip()
    if not task:
        raise RequestValidationError("task must not be blank.")
    return task


def _normalize_context(value: RequestContext | dict[str, Any]) -> RequestContext:
    if isinstance(value, RequestContext):
        return value
    if not isinstance(value, dict):
        raise RequestValidationError("context must be an object.")

    goal = value.get("goal")
    if goal is not None and not isinstance(goal, str):
        raise RequestValidationError("context.goal must be a string if provided.")

    return RequestContext(
        goal=goal.strip() if isinstance(goal, str) and goal.strip() else None,
        files=_normalize_string_list(value.get("files", []), "context.files"),
        constraints=_normalize_string_list(
            value.get("constraints", []), "context.constraints"
        ),
    )


def _normalize_config(value: RequestConfig | dict[str, Any]) -> RequestConfig:
    if isinstance(value, RequestConfig):
        return value
    if not isinstance(value, dict):
        raise RequestValidationError("config must be an object.")

    mode = value.get("mode", "plan_only")
    workflow = value.get("workflow", "auto")
    coverage_target = value.get("coverage_target", 0.8)
    model = value.get("model")
    provider = value.get("provider", "codex")
    models = value.get("models", {})
    providers = value.get("providers", {})

    if mode not in {"plan_only", "execute"}:
        raise RequestValidationError("config.mode must be 'plan_only' or 'execute'.")

    if workflow not in {"auto", "sequential", "parallel"}:
        raise RequestValidationError(
            "config.workflow must be 'auto', 'sequential', or 'parallel'."
        )

    if not isinstance(coverage_target, (int, float)):
        raise RequestValidationError("config.coverage_target must be a number.")

    coverage = float(coverage_target)
    if not 0.0 <= coverage <= 1.0:
        raise RequestValidationError("config.coverage_target must be between 0 and 1.")

    if provider not in {"codex", "claude"}:
        raise RequestValidationError("config.provider must be 'codex' or 'claude'.")

    # Validate models dict
    if not isinstance(models, dict):
        raise RequestValidationError("config.models must be an object.")
    for k, v in models.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise RequestValidationError("config.models must be a dict of string to string.")

    # Validate providers dict
    if not isinstance(providers, dict):
        raise RequestValidationError("config.providers must be an object.")
    for k, v in providers.items():
        if not isinstance(k, str) or not isinstance(v, str):
            raise RequestValidationError("config.providers must be a dict of string to string.")
        if v not in {"codex", "claude"}:
            raise RequestValidationError(f"config.providers['{k}'] must be 'codex' or 'claude'.")

    return RequestConfig(
        mode=mode,
        workflow=workflow,
        coverage_target=coverage,
        model=model,
        provider=provider,
        models=models,
        providers=providers,
    )


def _normalize_string_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise RequestValidationError(f"{field_name} must be an array of strings.")

    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RequestValidationError(f"{field_name} must be an array of strings.")
        stripped = item.strip()
        if stripped:
            normalized.append(stripped)
    return normalized

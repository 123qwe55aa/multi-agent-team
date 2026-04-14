from __future__ import annotations

import json
import logging
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from contracts import TeamRequest
from team_logger import TeamLogger

logger = logging.getLogger("multi-agent-team.orchestrator")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TeamState(Enum):
    INITIALIZING = "initializing"
    PLANNING = "planning"
    CODING = "coding"
    AUDIT = "audit"
    TESTING = "testing"
    BARRIER = "barrier"
    SYNTHESIZING = "synthesizing"
    CORRECTIVE = "corrective"
    FINALIZING = "finalizing"
    COMPLETE = "complete"
    ESCALATED = "escalated"
    WAITING_INPUT = "waiting_input"


class SubagentType(Enum):
    PM = "pm"
    CODING = "coding"
    AUDIT = "audit"
    TESTING = "testing"


class GateDecision(Enum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"


class EscalationType(Enum):
    AUTHORITY_GAP = "authority_gap"
    RISK = "risk"
    RESOURCE = "resource"
    CONFIDENCE = "confidence"
    AMBIGUITY = "ambiguity"


class Severity(Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AuditFinding:
    severity: Severity
    location: str
    description: str
    rule_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity.value,
            "location": self.location,
            "description": self.description,
            "rule_id": self.rule_id,
        }


@dataclass(frozen=True, slots=True)
class SubagentResult:
    agent_type: SubagentType
    status: str  # "success", "partial", "failed"
    files_changed: list[str] = field(default_factory=list)
    summary: str = ""
    confidence: float = 0.5
    findings: list[AuditFinding] = field(default_factory=list)
    coverage: float | None = None
    block_ship: bool = False
    risk_score: int = 0
    test_failures: list[str] = field(default_factory=list)
    escalation_triggers: list[EscalationType] = field(default_factory=list)
    requires_scope_change: bool = False
    tokens_exhausted: bool = False
    raw_output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_type": self.agent_type.value,
            "status": self.status,
            "files_changed": self.files_changed,
            "summary": self.summary,
            "confidence": self.confidence,
            "findings": [f.to_dict() for f in self.findings],
            "coverage": self.coverage,
            "block_ship": self.block_ship,
            "risk_score": self.risk_score,
            "test_failures": self.test_failures,
            "escalation_triggers": [e.value for e in self.escalation_triggers],
        }


@dataclass(frozen=True, slots=True)
class EscalationEvent:
    escalation_type: EscalationType
    severity: Severity
    reason: str
    action_taken: str = ""
    notified: bool = False
    blocking: bool = False
    decision_required: str = ""
    options: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.escalation_type.value,
            "severity": self.severity.value,
            "reason": self.reason,
            "actionTaken": self.action_taken,
            "notified": self.notified,
            "blocking": self.blocking,
            "decisionRequired": self.decision_required,
            "options": self.options,
        }


# ---------------------------------------------------------------------------
# Team Leader
# ---------------------------------------------------------------------------


@dataclass
class WorkflowConfig:
    workflow_type: str = "auto"  # "auto", "sequential", "parallel"
    coverage_target: float = 0.8
    audit_block_threshold: Severity = Severity.HIGH
    # Per-agent model selection
    # Example: {"coding": "MiniMax-M2.7", "audit": "gpt-5.4", "testing": "MiniMax-M2.7"}
    models: dict[str, str] = field(default_factory=dict)
    # Per-agent provider selection (default per type)
    # Codex: gpt-4o, gpt-5.4, o1, o3, o4-mini, etc.
    # Claude: MiniMax-M2.7 (via claude -m --print)
    providers: dict[str, str] = field(default_factory=dict)
    # Legacy: default model/provider (used if per-agent not specified)
    model: str | None = None
    provider: str = "codex"
    # Logging
    enable_logging: bool = True
    log_dir: str | None = None

    def get_model(self, agent_type: str) -> str | None:
        """Get model for specific agent type."""
        return self.models.get(agent_type, self.model)

    def get_provider(self, agent_type: str) -> str:
        """Get provider for specific agent type."""
        return self.providers.get(agent_type, self.provider)


@dataclass
class GateConfig:
    coverage_target: float = 0.8
    audit_block_threshold: str = "high"


@dataclass
class PMCriteria:
    """Acceptance criteria generated by PM agent."""
    id: str
    type: str  # "functional", "non_functional"
    description: str
    verification: str = ""
    priority: str = "MUST"  # "MUST", "SHOULD"
    metrics: dict | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "description": self.description,
            "verification": self.verification,
            "priority": self.priority,
            "metrics": self.metrics,
        }


@dataclass
class PMResult:
    """Result from PM agent."""
    requirement_summary: str
    criteria: list[PMCriteria]
    raw_output: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_summary": self.requirement_summary,
            "criteria": [c.to_dict() for c in self.criteria],
            "raw_output": self.raw_output,
        }


@dataclass
class TeamLeader:
    id: str
    task: str
    context: dict[str, Any]
    config: WorkflowConfig
    state: TeamState = TeamState.INITIALIZING
    results: dict[SubagentType, SubagentResult] = field(default_factory=dict)
    escalation: EscalationEvent | None = None
    findings_count: dict[str, int] = field(default_factory=lambda: {
        "critical": 0, "high": 0, "medium": 0, "low": 0
    })
    completed_phases: list[str] = field(default_factory=list)
    _correction_iterations: int = 0
    _max_corrections: int = 3
    pm_result: PMResult | None = None  # PM 产出的验收标准
    criteria: list[PMCriteria] = field(default_factory=list)  # 结构化验收标准

    def __init__(
        self,
        task: str,
        context: dict[str, Any] | None = None,
        config: WorkflowConfig | None = None,
        logger: TeamLogger | None = None,
    ) -> None:
        self.id = str(uuid.uuid4())[:8]
        self.task = task
        self.context = context or {}
        self.config = config or WorkflowConfig()
        self.state = TeamState.PLANNING
        self.results = {}
        self.escalation = None
        self.findings_count = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        self.completed_phases = []
        self._correction_iterations = 0
        self._logger = logger  # TeamLogger instance
        self._subagent_start_times: dict[str, float] = {}
        self._timeout_multipliers: dict[str, float] = {}  # agent_type -> multiplier
        # Use module-level logger for stdlib logging, self._logger for TeamLogger
        if logger is None:
            logging.getLogger("multi-agent-team.orchestrator").info(
                f"TeamLeader {self.id} initialized for task: {task[:50]}..."
            )

    @classmethod
    def from_dict(cls, data: dict[str, Any], config: WorkflowConfig | None = None, logger: TeamLogger | None = None) -> TeamLeader:
        """Restore TeamLeader from checkpoint dict."""
        tl = cls(
            task=data["task"],
            context=data.get("context", {}),
            config=config or WorkflowConfig(),
            logger=logger,
        )
        tl.id = data.get("id", tl.id)
        tl.state = TeamState(data.get("state", TeamState.PLANNING.value))
        tl.findings_count = data.get("findings_count", tl.findings_count)
        tl.completed_phases = data.get("completed_phases", [])
        tl._correction_iterations = data.get("_correction_iterations", 0)
        return tl

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict for checkpointing."""
        return {
            "id": self.id,
            "task": self.task,
            "context": self.context,
            "state": self.state.value,
            "results": {k.value: v.to_dict() for k, v in self.results.items()},
            "findings_count": self.findings_count,
            "completed_phases": self.completed_phases,
            "_correction_iterations": self._correction_iterations,
            "config": {
                "workflow_type": self.config.workflow_type,
                "coverage_target": self.config.coverage_target,
                "audit_block_threshold": self.config.audit_block_threshold.value,
                "model": self.config.model,
                "provider": self.config.provider,
                "models": self.config.models,
                "providers": self.config.providers,
                "enable_logging": self.config.enable_logging,
                "log_dir": self.config.log_dir,
            },
        }

    def save_checkpoint(self) -> None:
        """Save checkpoint for potential resume."""
        if self._logger:
            self._logger.log_checkpoint_saved(self.to_dict())

    def _restore_from_checkpoint(self, checkpoint_data: dict[str, Any]) -> None:
        """Restore state from checkpoint data."""
        self.id = checkpoint_data.get("id", self.id)
        self.state = TeamState(checkpoint_data.get("state", TeamState.PLANNING.value))
        self.findings_count = checkpoint_data.get("findings_count", self.findings_count)
        self.completed_phases = checkpoint_data.get("completed_phases", [])
        self._correction_iterations = checkpoint_data.get("_correction_iterations", 0)
        results_data = checkpoint_data.get("results", {})
        self.results = {}
        for k, v in results_data.items():
            agent_type = SubagentType(k)
            findings = [
                AuditFinding(
                    severity=Severity(f.get("severity", "medium")),
                    location=f.get("location", "unknown"),
                    description=f.get("description", ""),
                    rule_id=f.get("rule_id"),
                )
                for f in v.get("findings", [])
            ]
            self.results[agent_type] = SubagentResult(
                agent_type=agent_type,
                status=v.get("status", "partial"),
                files_changed=v.get("files_changed", []),
                summary=v.get("summary", ""),
                confidence=v.get("confidence", 0.5),
                findings=findings,
                coverage=v.get("coverage"),
                block_ship=v.get("block_ship", False),
                risk_score=v.get("risk_score", 0),
                test_failures=v.get("test_failures", []),
            )
        if self._logger:
            self._logger.log_checkpoint_restored(checkpoint_data)

    # ---------------------------------------------------------------------------
    # Subagent spawning
    # ---------------------------------------------------------------------------

    def _load_agent_prompt(self, agent_type: SubagentType) -> str:
        """Load system prompt for a subagent."""
        prompts_dir = Path(__file__).parent / "agents"
        filename = f"{agent_type.value}.md"
        path = prompts_dir / filename
        if path.exists():
            return path.read_text()
        # Fallback minimal prompts
        return self._default_prompt(agent_type)

    def _load_pm_prompt(self) -> str:
        """Load PM agent prompt."""
        pm_dir = Path(__file__).parent / "agents" / "pm"
        path = pm_dir / "PROMPT.md"
        if path.exists():
            return path.read_text()
        return self._default_pm_prompt()

    def _default_pm_prompt(self) -> str:
        """Default PM prompt if file not found."""
        return """You are a Product Manager. Generate acceptance criteria in JSON format ONLY.

## Rules
- Do NOT ask clarifying questions - if vague, make reasonable assumptions
- Output ONLY valid JSON, no other text before or after
- MUST = blocking, SHOULD = important but not blocking

## Output Format
{
  "requirement_summary": "One paragraph",
  "criteria": [
    {"id": "F1", "type": "functional", "description": "...", "verification": "...", "priority": "MUST|SHOULD"}
  ]
}
"""

    def _default_prompt(self, agent_type: SubagentType) -> str:
        prompts = {
            SubagentType.CODING: """You are a Coding/Debug expert agent.
Implement the given task following best practices.
Output a JSON object with: {"status": "success|partial|failed", "summary": "...", "files_changed": [...], "confidence": 0.0-1.0}
""",
            SubagentType.AUDIT: """You are a Security + Quality auditor agent.
Audit the code for security vulnerabilities, code quality issues, and best practice violations.
Output a JSON object with: {"status": "pass|warning|critical|failed", "findings": [...], "risk_score": 0-10, "block_ship": bool}
""",
            SubagentType.TESTING: """You are a Testing engineer agent.
Write and run tests, measure coverage.
Output a JSON object with: {"status": "all-pass|some-fail|all-fail", "coverage": 0.0-1.0, "block_ship": bool, "test_failures": [...]}
""",
        }
        return prompts.get(agent_type, "")

    def spawn_subagent(
        self, agent_type: SubagentType, task_suffix: str = ""
    ) -> SubagentResult:
        """Spawn a subagent via Codex CLI or Claude."""
        # Build context for subagent
        context_parts = []
        if self.context.get("goal"):
            context_parts.append(f"Goal: {self.context['goal']}")
        if self.context.get("files"):
            context_parts.append(f"Files: {', '.join(self.context['files'])}")
        if self.context.get("constraints"):
            context_parts.append(f"Constraints: {', '.join(self.context['constraints'])}")

        context_str = "\n".join(context_parts)
        full_task = f"{self.task}\n\n{context_str}\n\n{task_suffix}".strip()

        # Get per-agent provider and model
        agent_key = agent_type.value
        provider = self.config.get_provider(agent_key)
        model = self.config.get_model(agent_key)
        logger.info(f"TeamLeader {self.id} spawning {agent_type.value} via {provider} (model: {model})")

        # Log subagent spawn
        if self._logger:
            self._logger.log_subagent_spawn(agent_key, model, provider, full_task)

        self._subagent_start_times[agent_key] = time.monotonic()

        try:
            if provider == "codex":
                return self._spawn_codex(agent_type, full_task, model)
            elif provider == "claude":
                return self._spawn_claude(agent_type, full_task, model)
            else:
                return SubagentResult(
                    agent_type=agent_type,
                    status="failed",
                    summary=f"Unknown provider: {provider}",
                    confidence=0.0,
                )
        except subprocess.TimeoutExpired:
            logger.error(f"TeamLeader {self.id} {agent_type.value} agent timed out")
            if self._logger:
                self._logger.log_subagent_timeout(agent_key, self._calculate_timeout(agent_type, full_task))
            # Auto-extend timeout for this agent type on timeout
            current_mult = self._timeout_multipliers.get(agent_key, 1.0)
            self.extend_timeout(agent_key, current_mult * 2.0)
            return SubagentResult(
                agent_type=agent_type,
                status="timeout_retry",
                summary=f"Agent timed out after {self._calculate_timeout(agent_type, full_task)}s. Timeout extended to {int(current_mult * 2.0)}x for retry. No partial output available.",
                confidence=0.0,
                escalation_triggers=[EscalationType.RESOURCE],
            )
        except Exception as exc:
            logger.exception(f"TeamLeader {self.id} {agent_type.value} agent error")
            if self._logger:
                self._logger.log_subagent_error(agent_key, str(exc))
            return SubagentResult(
                agent_type=agent_type,
                status="failed",
                summary=f"Agent error: {exc}",
                confidence=0.0,
            )

    def _spawn_codex(self, agent_type: SubagentType, full_task: str, model: str | None) -> SubagentResult:
        """Spawn via Codex CLI."""
        # Build command base with optional model config
        # Codex uses: codex -c model="<model>" <subcommand> [prompt]
        cmd_base = ["codex"]
        if model:
            # Codex uses -c model="<model>" format
            cmd_base.extend(["-c", f'model="{model}"'])

        if agent_type == SubagentType.CODING:
            cmd = cmd_base + ["exec", "--skip-git-repo-check"]
            prompt = f"""You are a coding expert. {full_task}

IMPORTANT: When done, output a JSON block with your results:
{{"status": "success|partial|failed", "summary": "...", "files_changed": [...], "confidence": 0.0-1.0}}"""

        elif agent_type == SubagentType.AUDIT:
            cmd = cmd_base + ["review", "--skip-git-repo-check"]
            prompt = f"""{full_task}

IMPORTANT: When done, output a JSON block with your findings:
{{"status": "pass|warning|critical|failed", "findings": [{{"severity": "critical|high|medium|low", "location": "...", "description": "..."}}], "risk_score": 0-10, "block_ship": false}}"""

        elif agent_type == SubagentType.TESTING:
            cmd = cmd_base + ["exec", "--skip-git-repo-check"]
            prompt = f"""You are a testing engineer. {full_task}

IMPORTANT: When done, output a JSON block with your results:
{{"status": "all-pass|some-fail|all-fail", "coverage": 0.0-1.0, "block_ship": false, "test_failures": []}}"""
        else:
            cmd = cmd_base + ["exec", "--skip-git-repo-check"]
            prompt = full_task

        full_cmd = cmd + [prompt]

        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=self._calculate_timeout(agent_type, full_task),
            cwd=self.context.get("cwd", str(Path.cwd())),
        )

        output = result.stdout.strip()
        if not output and result.stderr:
            output = result.stderr.strip()

        logger.info(f"TeamLeader {self.id} {agent_type.value} (Codex) completed")
        return self._parse_subagent_result(agent_type, output)

    def _calculate_timeout(self, agent_type: SubagentType, task: str) -> int:
        """Calculate timeout based on agent type and task complexity.

        Strategy:
        - Base timeout by agent type
        - Scale by task length (chars)
        - Scale by keyword indicators of complexity
        """
        # Base timeout by agent type (seconds)
        BASE_TIMEOUTS = {
            SubagentType.PM: 180,      # PM usually simpler
            SubagentType.CODING: 300,   # Coding needs more time
            SubagentType.AUDIT: 240,    # Audit moderate
            SubagentType.TESTING: 300,   # Testing moderate
        }
        base = BASE_TIMEOUTS.get(agent_type, 300)

        # Task complexity multipliers
        complexity = 1.0

        # Scale by task length
        task_len = len(task)
        if task_len > 5000:
            complexity += 0.5
        elif task_len > 2000:
            complexity += 0.25

        # Keywords indicating larger scope
        large_scope_keywords = [
            # General large scope
            "implement", "create", "build", "develop", "full-stack",
            "complete", "entire", "system", "architecture",
            "database", "migration", "refactor", "multiple",
            # Frontend-specific (common in this workflow)
            "react", "vue", "angular", "frontend", "typescript", "javascript",
            "component", "page", "pages", "routing", "tailwind", "css",
            "frontend", "ui", "interface", "dashboard", "application",
            # Project structure indicators
            "project", "structure", "directory", "folder", "file",
            "module", "service", "store", "hook", "api",
        ]
        keyword_count = sum(1 for kw in large_scope_keywords if kw.lower() in task.lower())
        complexity += keyword_count * 0.12

        # Phase keywords (Phase 1, Phase 2, etc.)
        import re
        phase_count = len(re.findall(r'phase\s*\d', task.lower()))
        if phase_count > 1:
            complexity += 0.3

        timeout = int(base * complexity)

        # Apply timeout multiplier if set (supports 'all' wildcard)
        # Per-agent multiplier overrides 'all' wildcard
        all_multiplier = self._timeout_multipliers.get("all", 1.0)
        specific_multiplier = self._timeout_multipliers.get(agent_type.value, all_multiplier)
        timeout = int(timeout * specific_multiplier)

        # Clamp: min 60s, max 900s (15 min)
        return max(60, min(900, timeout))

    def extend_timeout(self, scope_key: str, multiplier: float) -> None:
        """Extend timeout for a specific scope key (e.g. agent type).

        Args:
            scope_key: The scope identifier (e.g. "coding", "pm", "all").
            multiplier: Timeout multiplier (must be > 0, finite number).

        Raises:
            ValueError: If multiplier is not a finite positive number.
        """
        # Validate multiplier: must be a number, finite (not NaN/Infinity), and > 0
        if not isinstance(multiplier, (int, float)):
            raise ValueError(f"Multiplier must be a number, got {type(multiplier).__name__}")
        import math
        if not math.isfinite(multiplier):
            raise ValueError(f"Multiplier must be finite, got {multiplier}")
        if not (multiplier > 0):
            raise ValueError(f"Multiplier must be > 0, got {multiplier}")

        self._timeout_multipliers[scope_key] = multiplier
        logger.info(f"TeamLeader {self.id} extended {scope_key} timeout by {multiplier}x")

    def reset_timeout_multiplier(self, scope_key: str) -> None:
        """Reset timeout multiplier for a scope key back to default (1.0)."""
        if scope_key in self._timeout_multipliers:
            del self._timeout_multipliers[scope_key]
            logger.info(f"TeamLeader {self.id} reset {scope_key} timeout multiplier to 1.0")

    def _breakdown_task(self) -> str:
        """Break down the task into implementation guidance for coding agent.

        Analyzes the task and criteria to provide:
        - Implementation steps (ordered)
        - Architecture decisions
        - File structure hints
        """
        lines = ["## Implementation Guidance"]

        # Detect scope from task keywords
        task_lower = self.task.lower()
        lines.append("### Suggested Implementation Steps")
        steps = []

        # Backend indicators
        if any(kw in task_lower for kw in ["backend", "后端", "api", "server", "scraper", "爬虫"]):
            steps.append("1. Create backend schema/models for the new feature")
            steps.append("2. Implement service layer (data fetching, caching)")
            steps.append("3. Add API endpoint(s) with proper error handling")
            steps.append("4. Register routes in main router")

        # Frontend indicators
        if any(kw in task_lower for kw in ["frontend", "前端", "page", "页面", "ui", "react"]):
            steps.append("5. Create TypeScript types/interfaces")
            steps.append("6. Build API service layer")
            steps.append("7. Create custom hooks for data fetching")
            steps.append("8. Implement UI components")
            steps.append("9. Add page/route and integrate")

        # Data/caching
        if any(kw in task_lower for kw in ["redis", "cache", "缓存"]):
            steps.append("10. Implement Redis caching with TTL")

        if not steps:
            steps.append("1. Analyze requirements and identify files to create/modify")
            steps.append("2. Implement backend if needed")
            steps.append("3. Implement frontend if needed")
            steps.append("4. Test integration")

        for step in steps:
            lines.append(step)

        # Architecture hints
        lines.append("\n### Architecture Notes")
        if "scraper" in task_lower or "爬虫" in task_lower:
            lines.append("- Use server-side scraping (not client-side, avoids CORS)")
            lines.append("- Cache scraped data in Redis with TTL")
            lines.append("- Parse HTML with BeautifulSoup or similar")
        if "champion" in task_lower or "build" in task_lower:
            lines.append("- Champion build data: separate route/role dimension")
            lines.append("- Item images: use DataDragon CDN")
            lines.append("- Rune icons: use DataDragon CDN")

        # File structure hint
        lines.append("\n### Expected File Changes")
        if "backend" in task_lower or "后端" in task_lower:
            lines.append("- app/schemas/<feature>.py (Pydantic models)")
            lines.append("- app/services/<feature>_service.py (business logic)")
            lines.append("- app/api/endpoints/<feature>.py (routes)")
        if "frontend" in task_lower or "前端" in task_lower:
            lines.append("- src/types/<feature>.ts (TypeScript types)")
            lines.append("- src/services/<feature>Api.ts (API calls)")
            lines.append("- src/hooks/use<Feature>.ts (data hooks)")
            lines.append("- src/pages/<Feature>Page.tsx (main page)")
            lines.append("- src/components/<Component>.tsx (reusable components)")

        return "\n".join(lines)

    def _spawn_claude(self, agent_type: SubagentType, full_task: str, model: str | None) -> SubagentResult:
        """Spawn via Claude Code CLI."""
        # Build command base
        cmd_parts = ["claude"]
        if model:
            cmd_parts.extend(["--model", model])
        cmd_parts.extend(["--dangerously-skip-permissions", "--print"])

        if agent_type == SubagentType.CODING:
            prompt = f"""You are a coding expert. {full_task}

IMPORTANT: When done, output a JSON block with your results:
{{"status": "success|partial|failed", "summary": "...", "files_changed": [...], "confidence": 0.0-1.0}}"""

        elif agent_type == SubagentType.AUDIT:
            prompt = f"""You are a code auditor. {full_task}

IMPORTANT: When done, output a JSON block with your findings:
{{"status": "pass|warning|critical|failed", "findings": [{{"severity": "critical|high|medium|low", "location": "...", "description": "..."}}], "risk_score": 0-10, "block_ship": false}}"""

        elif agent_type == SubagentType.TESTING:
            prompt = f"""You are a testing engineer. {full_task}

IMPORTANT: When done, output a JSON block with your results:
{{"status": "all-pass|some-fail|all-fail", "coverage": 0.0-1.0, "block_ship": false, "test_failures": []}}"""
        else:
            prompt = full_task

        full_cmd = cmd_parts + [prompt]

        result = subprocess.run(
            full_cmd,
            capture_output=True,
            text=True,
            timeout=self._calculate_timeout(agent_type, full_task),
            cwd=self.context.get("cwd", str(Path.cwd())),
        )

        output = result.stdout.strip()
        if not output and result.stderr:
            output = result.stderr.strip()

        logger.info(f"TeamLeader {self.id} {agent_type.value} (Claude) completed")
        return self._parse_subagent_result(agent_type, output)

    def spawn_pm(self) -> PMResult:
        """Spawn PM agent to generate acceptance criteria."""
        import json

        logger.info(f"TeamLeader {self.id} spawning PM agent")
        pm_prompt = self._load_pm_prompt()
        full_task = f"{self.task}\n\n{pm_prompt}"

        # PM 使用 codex 或 claude 运行
        provider = self.config.get_provider("pm")
        model = self.config.get_model("pm")

        try:
            if provider == "codex":
                cmd = ["codex", "exec", "--skip-git-repo-check"]
                if model:
                    cmd.extend(["-c", f'model="{model}"'])
                cmd.append(full_task)
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self._calculate_timeout(SubagentType.PM, full_task),
                    cwd=self.context.get("cwd", str(Path.cwd())),
                )
                output = result.stdout.strip() or result.stderr.strip()
            else:
                cmd = ["claude"]
                if model:
                    cmd.extend(["--model", model])
                cmd.extend(["--dangerously-skip-permissions", "--print"])
                cmd.append(full_task)
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self._calculate_timeout(SubagentType.PM, full_task),
                    cwd=self.context.get("cwd", str(Path.cwd())),
                )
                output = result.stdout.strip() or result.stderr.strip()

            logger.info(f"TeamLeader {self.id} PM agent completed")

        except subprocess.TimeoutExpired:
            logger.error(f"TeamLeader {self.id} PM agent timed out")
            return PMResult(
                requirement_summary=self.task,
                criteria=[],
                raw_output="PM agent timed out",
            )
        except Exception as exc:
            logger.exception(f"TeamLeader {self.id} PM agent error")
            return PMResult(
                requirement_summary=self.task,
                criteria=[],
                raw_output=f"PM agent error: {exc}",
            )

        # 解析 PM 输出
        try:
            # 提取 JSON
            json_str = output
            if "```json" in output:
                start = output.find("```json") + 7
                end = output.find("```", start)
                if end > start:
                    json_str = output[start:end].strip()
            elif "{" in output and "}" in output:
                start = output.find("{")
                end = output.rfind("}") + 1
                json_str = output[start:end]

            data = json.loads(json_str)

            # Parse functional criteria
            criteria = [
                PMCriteria(
                    id=c.get("id", f"F{i}"),
                    type=c.get("type", "functional"),
                    description=c.get("description", ""),
                    verification=c.get("verification", ""),
                    priority=c.get("priority", "MUST"),
                    metrics=c.get("metrics"),
                )
                for i, c in enumerate(data.get("criteria", []))
            ]

            # Parse non_functional criteria and merge
            non_functional = data.get("non_functional", [])
            nf_criteria = [
                PMCriteria(
                    id=c.get("id", f"N{i}"),
                    type="non_functional",
                    description=c.get("description", ""),
                    verification=c.get("verification", ""),
                    priority=c.get("priority", "MUST"),
                    metrics=c.get("metrics"),
                )
                for i, c in enumerate(non_functional)
            ]
            criteria.extend(nf_criteria)

            return PMResult(
                requirement_summary=data.get("requirement_summary", self.task),
                criteria=criteria,
                raw_output=output,
            )
        except json.JSONDecodeError:
            logger.warning(f"Could not parse PM JSON, using task as summary")
            return PMResult(
                requirement_summary=self.task,
                criteria=[],
                raw_output=output,
            )

    def _parse_subagent_result(
        self, agent_type: SubagentType, raw_output: str
    ) -> SubagentResult:
        """Parse subagent output into SubagentResult."""
        import json

        # Try to extract JSON from output
        json_str = raw_output
        if "```json" in raw_output:
            start = raw_output.find("```json") + 7
            end = raw_output.find("```", start)
            if end > start:
                json_str = raw_output[start:end].strip()
        elif "```" in raw_output:
            start = raw_output.find("```") + 3
            end = raw_output.find("```", start)
            if end > start:
                json_str = raw_output[start:end].strip()

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            logger.warning(f"Could not parse JSON from output, using raw output")
            return SubagentResult(
                agent_type=agent_type,
                status="partial",
                summary=raw_output[:500],
                confidence=0.3,
            )

        # Parse based on agent type
        if agent_type == SubagentType.CODING:
            findings = [
                AuditFinding(
                    severity=Severity(f.get("severity", "medium")),
                    location=f.get("location", "unknown"),
                    description=f.get("description", ""),
                )
                for f in data.get("findings", [])
            ]
            return SubagentResult(
                agent_type=agent_type,
                status=data.get("status", "partial"),
                files_changed=data.get("files_changed", []),
                summary=data.get("summary", ""),
                confidence=data.get("confidence", 0.5),
                findings=findings,
                raw_output=raw_output,
            )

        elif agent_type == SubagentType.AUDIT:
            findings = [
                AuditFinding(
                    severity=Severity(f.get("severity", "medium")),
                    location=f.get("location", "unknown"),
                    description=f.get("description", ""),
                    rule_id=f.get("rule_id"),
                )
                for f in data.get("findings", [])
            ]
            # Count findings by severity
            counts = {"critical": 0, "high": 0, "medium": 0, "low": 0}
            for f in findings:
                if f.severity in counts:
                    counts[f.severity.value] += 1

            return SubagentResult(
                agent_type=agent_type,
                status=data.get("status", "warning"),
                findings=findings,
                risk_score=data.get("risk_score", 0),
                block_ship=data.get("block_ship", False),
                confidence=data.get("confidence", 0.5),
                raw_output=raw_output,
            )

        elif agent_type == SubagentType.TESTING:
            return SubagentResult(
                agent_type=agent_type,
                status=data.get("status", "some-fail"),
                coverage=data.get("coverage", 0.0),
                block_ship=data.get("block_ship", False),
                test_failures=data.get("test_failures", []),
                confidence=data.get("confidence", 0.5),
                raw_output=raw_output,
            )

        return SubagentResult(
            agent_type=agent_type,
            status="partial",
            summary=raw_output[:500],
            confidence=0.3,
        )

    def _log_subagent_completion(self, agent_type: str, result: SubagentResult) -> None:
        """Log subagent completion to TeamLogger."""
        if not self._logger:
            return

        # Calculate duration
        duration_ms = 0
        start_time = self._subagent_start_times.get(agent_type)
        if start_time:
            duration_ms = int((time.monotonic() - start_time) * 1000)

        # Count findings by severity
        findings_count: dict[str, int] = {}
        for f in result.findings:
            sev = f.severity.value
            findings_count[sev] = findings_count.get(sev, 0) + 1

        self._logger.log_subagent_complete(
            agent_type=agent_type,
            status=result.status,
            summary=result.summary,
            duration_ms=duration_ms,
            files_changed=result.files_changed,
            findings_count=findings_count,
            coverage=result.coverage,
            risk_score=result.risk_score,
            test_failures=result.test_failures,
            raw_output_truncated=result.raw_output[:2000] if result.raw_output else None,
        )

    # ---------------------------------------------------------------------------
    # Gate evaluation
    # ---------------------------------------------------------------------------

    def evaluate_audit_gate(self, result: SubagentResult) -> GateDecision:
        """Evaluate audit gate: pass if risk acceptable."""
        if result.status in ("failed", "critical"):
            return GateDecision.FAIL
        if result.block_ship:
            return GateDecision.FAIL
        # Check if risk score exceeds threshold
        threshold_map = {
            Severity.CRITICAL: 3,
            Severity.HIGH: 5,
            Severity.MEDIUM: 7,
            Severity.LOW: 10,
        }
        threshold = threshold_map.get(self.config.audit_block_threshold, 5)
        if result.risk_score >= threshold:
            return GateDecision.FAIL
        return GateDecision.PASS

    def evaluate_testing_gate(self, result: SubagentResult) -> GateDecision:
        """Evaluate testing gate: pass if coverage sufficient and all pass."""
        if result.status in ("all-fail", "some-fail"):
            return GateDecision.FAIL
        if result.coverage is not None and result.coverage < self.config.coverage_target:
            return GateDecision.FAIL
        if result.block_ship:
            return GateDecision.FAIL
        return GateDecision.PASS

    # ---------------------------------------------------------------------------
    # Escalation handling
    # ---------------------------------------------------------------------------

    def check_escalation(self, result: SubagentResult) -> EscalationEvent | None:
        """Check if result triggers escalation."""
        # Critical findings - highest priority, always escalate
        for finding in result.findings:
            if finding.severity == Severity.CRITICAL:
                return EscalationEvent(
                    escalation_type=EscalationType.RISK,
                    severity=Severity.CRITICAL,
                    reason=f"Critical finding: {finding.description}",
                    action_taken="Coding agent will fix automatically",
                    notified=True,
                    blocking=False,
                )

        # Authority gap - blocking, must wait for Main Agent
        if result.requires_scope_change:
            return EscalationEvent(
                escalation_type=EscalationType.AUTHORITY_GAP,
                severity=Severity.HIGH,
                reason="Subagent requires scope change",
                action_taken="Paused and waiting for Main Agent decision",
                blocking=True,
                decision_required="Approve scope change?",
                options=["Approve", "Reject", "Modify"],
            )

        # Resource exhaustion
        if result.tokens_exhausted:
            return EscalationEvent(
                escalation_type=EscalationType.RESOURCE,
                severity=Severity.MEDIUM,
                reason="Token budget exhausted",
                action_taken="Terminating and returning current results",
                notified=True,
            )

        # Confidence low - only escalate if max corrections reached
        if result.confidence < 0.6 and result.status != "success":
            if self._correction_iterations >= self._max_corrections:
                return EscalationEvent(
                    escalation_type=EscalationType.CONFIDENCE,
                    severity=Severity.MEDIUM,
                    reason=f"Low confidence ({result.confidence}) after max corrections",
                    action_taken="Returning with partial results",
                    notified=True,
                )

        return None

    # ---------------------------------------------------------------------------
    # Main workflow
    # ---------------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """Execute the full team workflow."""
        logger.info(f"TeamLeader {self.id} starting workflow")

        # Phase 0: PM - Generate acceptance criteria
        self.state = TeamState.PLANNING
        self.completed_phases.append("pm")
        if self._logger:
            self._logger.log_pm_start()
        pm_result = self.spawn_pm()
        self.pm_result = pm_result
        self.criteria = pm_result.criteria
        logger.info(f"TeamLeader {self.id} PM generated {len(self.criteria)} criteria")
        if self._logger:
            self._logger.log_pm_complete(len(self.criteria), pm_result.raw_output)
        self.save_checkpoint()

        # Build criteria context for subagents
        criteria_context = ""
        if self.criteria:
            criteria_lines = ["## Acceptance Criteria"]
            for c in self.criteria:
                criteria_lines.append(f"- [{c.id}] {c.description} (Priority: {c.priority})")
                if c.verification:
                    criteria_lines.append(f"  Verification: {c.verification}")
            criteria_context = "\n".join(criteria_lines)

        # TeamLeader breakdown: add implementation guidance
        breakdown_context = self._breakdown_task()

        # Combine: task + criteria + breakdown
        full_coding_context = f"{self.task}\n\n{criteria_context}\n\n{breakdown_context}"

        # Phase 1: Coding (with auto-retry on timeout)
        self.state = TeamState.CODING
        self.completed_phases.append("coding")
        coding_retries = 0
        while True:
            if self._logger:
                self._logger.log_phase_start("coding")
            coding_result = self.spawn_subagent(
                SubagentType.CODING,
                task_suffix=f"\n\n{criteria_context}\n\n{breakdown_context}"
            )
            self.results[SubagentType.CODING] = coding_result
            self._log_subagent_completion("coding", coding_result)

            # Auto-retry on timeout_retry status
            if coding_result.status == "timeout_retry":
                coding_retries += 1
                logger.info(f"TeamLeader {self.id} coding timed out, retry {coding_retries} with extended timeout")
                if coding_retries >= 3:
                    logger.warning(f"TeamLeader {self.id} max coding retries reached")
                    break
                continue  # Retry with extended timeout
            break  # Normal exit

        # Check escalation
        escalation = self.check_escalation(coding_result)
        if escalation:
            if escalation.blocking:
                self.state = TeamState.WAITING_INPUT
                self.escalation = escalation
                if self._logger:
                    self._logger.log_escalation_raised(escalation.to_dict())
                return self._build_escalation_response()
            else:
                self.escalation = escalation
                if self._logger:
                    self._logger.log_escalation_raised(escalation.to_dict())

        # Phase 2: Testing (with auto-retry on timeout)
        self.state = TeamState.TESTING
        self.completed_phases.append("testing")
        testing_retries = 0
        while True:
            if self._logger:
                self._logger.log_phase_start("testing")
            testing_result = self.spawn_subagent(
                SubagentType.TESTING,
                task_suffix=f"Files to test: {', '.join(coding_result.files_changed) if coding_result.files_changed else 'none yet - coding may still be running'}"
            )
            self.results[SubagentType.TESTING] = testing_result
            self._log_subagent_completion("testing", testing_result)

            # Auto-retry on timeout_retry status
            if testing_result.status == "timeout_retry" and coding_result.files_changed:
                testing_retries += 1
                logger.info(f"TeamLeader {self.id} testing timed out, retry {testing_retries} with extended timeout")
                if testing_retries >= 3:
                    logger.warning(f"TeamLeader {self.id} max testing retries reached")
                    break
                continue  # Retry with extended timeout
            break  # Normal exit

        # Testing gate
        test_gate = self.evaluate_testing_gate(testing_result)
        if self._logger:
            self._logger.log_gate_decision("testing", test_gate.value, {
                "coverage": testing_result.coverage,
                "block_ship": testing_result.block_ship,
                "test_failures_count": len(testing_result.test_failures),
            })

        # Test FAIL corrective loop: Coding → Testing (NOT Audit)
        if test_gate == GateDecision.FAIL:
            self.state = TeamState.CORRECTIVE
            if self._correction_iterations < self._max_corrections:
                self._correction_iterations += 1
                logger.info(f"TeamLeader {self.id} testing failed, corrective iteration {self._correction_iterations}")
                if self._logger:
                    self._logger.log_correction_iteration(self._correction_iterations, "test_gate_fail")
                failures_str = ", ".join(testing_result.test_failures)
                # Fix: Coding → Testing (skip Audit)
                coding_result = self.spawn_subagent(
                    SubagentType.CODING,
                    task_suffix=f"Fix failing tests: {failures_str}"
                )
                self.results[SubagentType.CODING] = coding_result
                self._log_subagent_completion("coding", coding_result)
                # Re-run testing
                testing_result = self.spawn_subagent(
                    SubagentType.TESTING,
                    task_suffix=f"Files to test: {', '.join(coding_result.files_changed)}"
                )
                self.results[SubagentType.TESTING] = testing_result
                self._log_subagent_completion("testing", testing_result)
                test_gate = self.evaluate_testing_gate(testing_result)
                if self._logger:
                    self._logger.log_gate_decision("testing", test_gate.value, {"post_corrective": True})
            else:
                self.escalation = EscalationEvent(
                    escalation_type=EscalationType.RISK,
                    severity=Severity.HIGH,
                    reason="Max correction iterations reached, testing still failing",
                    action_taken="Returning results for manual review",
                    notified=True,
                )
                if self._logger:
                    self._logger.log_escalation_raised(self.escalation.to_dict())
        else:
            if self._logger:
                self._logger.log_gate_pass("testing")

        # Phase 3: Audit (only if Testing PASSED)
        # Audit gate: only run if test passed, skip audit if still in correction
        audit_gate = GateDecision.SKIP  # default
        if test_gate != GateDecision.FAIL and not self.escalation:
            self.state = TeamState.AUDIT
            self.completed_phases.append("audit")
            if self._logger:
                self._logger.log_phase_start("audit")
            audit_result = self.spawn_subagent(
                SubagentType.AUDIT,
                task_suffix=f"Files changed: {', '.join(coding_result.files_changed)}"
            )
            self.results[SubagentType.AUDIT] = audit_result
            self._log_subagent_completion("audit", audit_result)

            # Update findings count
            for f in audit_result.findings:
                if f.severity.value in self.findings_count:
                    self.findings_count[f.severity.value] += 1

            # Audit gate
            audit_gate = self.evaluate_audit_gate(audit_result)
            if self._logger:
                self._logger.log_gate_decision("audit", audit_gate.value, {
                    "risk_score": audit_result.risk_score,
                    "block_ship": audit_result.block_ship,
                })

            # Audit FAIL corrective loop: Coding → Testing → Audit
            if audit_gate == GateDecision.FAIL:
                self.state = TeamState.CORRECTIVE
                if self._correction_iterations < self._max_corrections:
                    self._correction_iterations += 1
                    logger.info(f"TeamLeader {self.id} audit failed, corrective iteration {self._correction_iterations}")
                    if self._logger:
                        self._logger.log_correction_iteration(self._correction_iterations, "audit_gate_fail")
                    findings_str = "; ".join(
                        f"{f.severity.value}: {f.description}" for f in audit_result.findings
                    )
                    # Fix: Coding → Testing → Audit
                    coding_result = self.spawn_subagent(
                        SubagentType.CODING,
                        task_suffix=f"Fix audit issues: {findings_str}"
                    )
                    self.results[SubagentType.CODING] = coding_result
                    self._log_subagent_completion("coding", coding_result)
                    # Re-run testing
                    testing_result = self.spawn_subagent(
                        SubagentType.TESTING,
                        task_suffix=f"Files to test: {', '.join(coding_result.files_changed)}"
                    )
                    self.results[SubagentType.TESTING] = testing_result
                    self._log_subagent_completion("testing", testing_result)
                    test_gate = self.evaluate_testing_gate(testing_result)
                    if self._logger:
                        self._logger.log_gate_decision("testing", test_gate.value, {"post_corrective": True})
                    if test_gate == GateDecision.PASS:
                        # Test passed, re-run audit
                        audit_result = self.spawn_subagent(
                            SubagentType.AUDIT,
                            task_suffix=f"Files changed: {', '.join(coding_result.files_changed)}"
                        )
                        self.results[SubagentType.AUDIT] = audit_result
                        self._log_subagent_completion("audit", audit_result)
                        audit_gate = self.evaluate_audit_gate(audit_result)
                        if self._logger:
                            self._logger.log_gate_decision("audit", audit_gate.value, {"post_corrective": True})
                    else:
                        audit_gate = GateDecision.FAIL
                else:
                    self.escalation = EscalationEvent(
                        escalation_type=EscalationType.RISK,
                        severity=Severity.HIGH,
                        reason="Max correction iterations reached, audit still failing",
                        action_taken="Returning results for manual review",
                        notified=True,
                    )
                    if self._logger:
                        self._logger.log_escalation_raised(self.escalation.to_dict())
            else:
                if self._logger:
                    self._logger.log_gate_pass("audit")

        # Finalize
        self.state = TeamState.FINALIZING
        if self._logger:
            self._logger.log_phase_end("finalizing" if self.completed_phases else "testing", next_phase="finalizing")
        self.save_checkpoint()

        # Determine overall gate decision
        if self.escalation and self.escalation.blocking:
            gate = "escalate"
        elif test_gate == GateDecision.FAIL or audit_gate == GateDecision.FAIL:
            gate = "fail"
        else:
            gate = "pass"

        self.state = TeamState.COMPLETE

        if self._logger:
            self._logger.log_phase_end("finalizing", next_phase="complete")
            self._logger.log_workflow_complete(gate)

        return self._build_final_response(gate)

    def resume(self, instruction: str) -> dict[str, Any]:
        """Resume from escalation with Main Agent instruction."""
        logger.info(f"TeamLeader {self.id} resuming with instruction: {instruction[:50]}...")
        self.escalation = None
        self.state = TeamState.CORRECTIVE

        # Main Agent has given direction, continue workflow
        # For now, just mark as waiting_input resolved and continue
        return self.run()

    # ---------------------------------------------------------------------------
    # Response builders
    # ---------------------------------------------------------------------------

    def _build_final_response(self, gate: str) -> dict[str, Any]:
        """Build final response dict."""
        coding = self.results.get(SubagentType.CODING)
        testing = self.results.get(SubagentType.TESTING)

        summary_parts = []
        if coding:
            summary_parts.append(f"Coding: {coding.summary or coding.status}")
        if testing and testing.coverage is not None:
            summary_parts.append(f"Coverage: {testing.coverage:.0%}")
        summary_parts.append(f"Findings: {sum(self.findings_count.values())}")

        return {
            "summary": "; ".join(summary_parts) if summary_parts else "No results",
            "gateDecision": gate,
            "completedPhases": self.completed_phases,
            "findings": self.findings_count,
            "escalation": self.escalation.to_dict() if self.escalation else None,
            "nextAction": (
                "Review and approve" if gate == "pass"
                else "Fix issues and retry" if gate == "fail"
                else "Provide decision on escalation"
            ),
            "pm": {
                "requirement_summary": self.pm_result.requirement_summary if self.pm_result else None,
                "criteria": [c.to_dict() for c in self.criteria] if self.criteria else [],
            },
        }

    def _build_escalation_response(self) -> dict[str, Any]:
        """Build escalation response when blocked."""
        return {
            "summary": f"Team Leader paused: {self.escalation.reason if self.escalation else 'Awaiting input'}",
            "gateDecision": "escalate",
            "completedPhases": self.completed_phases,
            "findings": self.findings_count,
            "escalation": self.escalation.to_dict() if self.escalation else None,
            "nextAction": (
                f"Provide decision: {', '.join(self.escalation.options)}"
                if self.escalation and self.escalation.options
                else "Provide instruction to continue"
            ),
            "session_id": self.id,
        }


# ---------------------------------------------------------------------------
# Orchestrator (updated to use TeamLeader)
# ---------------------------------------------------------------------------


@dataclass
class Orchestrator:
    """Main orchestrator facade."""

    def run(self, request: TeamRequest, timeout_multipliers: dict[str, float] | None = None) -> dict[str, Any]:
        """Run team workflow.

        Args:
            request: The team request with task and config
            timeout_multipliers: Optional dict of agent_type -> multiplier, e.g. {"coding": 2.0}
        """
        if request.config.mode == "plan_only":
            return self._build_plan_response(request)

        # Execute mode
        config = WorkflowConfig(
            workflow_type=request.config.workflow,
            coverage_target=request.config.coverage_target,
            model=request.config.model,
            provider=request.config.provider,
            models=request.config.models,
            providers=request.config.providers,
            enable_logging=True,
            log_dir=None,  # Use default ~/.multi-agent-team/logs
        )
        context_dict = {
            "goal": request.context.goal,
            "files": request.context.files,
            "constraints": request.context.constraints,
        }

        # Create TeamLogger
        team_logger: TeamLogger | None = None
        if config.enable_logging:
            try:
                team_logger = TeamLogger(
                    base_dir=config.log_dir,
                    task=request.task,
                )
            except Exception as exc:
                logging.warning(f"Failed to create TeamLogger: {exc}")

        team_leader = TeamLeader(
            task=request.task,
            context=context_dict,
            config=config,
            logger=team_logger,
        )

        # Apply timeout multipliers if provided
        if timeout_multipliers:
            for agent_type, multiplier in timeout_multipliers.items():
                team_leader.extend_timeout(agent_type, multiplier)

        result = team_leader.run()

        # Attach run_id and log path to result
        if team_logger:
            result["run_id"] = team_logger.run_id
            result["log_dir"] = str(team_logger.run_dir)
            team_logger.close()

        return result

    def _build_plan_response(self, request: TeamRequest) -> dict[str, Any]:
        """Build planning response."""
        file_hint = (
            f" across {len(request.context.files)} file(s)"
            if request.context.files
            else ""
        )
        summary = (
            f"Accepted task '{request.task}'{file_hint}. "
            f"Will spawn Team Leader to orchestrate Coding → Audit → Testing workflow."
        )
        return {
            "summary": summary,
            "gateDecision": "not_run",
            "completedPhases": ["intake", "planning"],
            "findings": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "escalation": None,
            "nextAction": "Use mode='execute' to run the full workflow.",
            "plan": {
                "workflow": request.config.workflow,
                "mode": request.config.mode,
                "coverageTarget": request.config.coverage_target,
                "task": request.task,
                "goal": request.context.goal,
                "files": request.context.files,
                "constraints": request.context.constraints,
            },
        }

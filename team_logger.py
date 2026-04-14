"""TeamLogger: Structured logging for multi-agent-team workflow supervision.

Log structure:
    logs/
    └── runs/
        └── {run_id}/
            ├── audit.jsonl       # Phase transitions, gate decisions, escalations
            ├── subagents.jsonl   # Per-agent input/output tracking
            └── summary.json      # Final run summary (written on completion)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger("multi-agent-team.team_logger")


# ---------------------------------------------------------------------------
# Event Types
# ---------------------------------------------------------------------------


class EventType(Enum):
    # Phase transitions
    PHASE_START = "phase_start"
    PHASE_END = "phase_end"
    PHASE_SKIP = "phase_skip"

    # Gate decisions
    GATE_DECISION = "gate_decision"
    GATE_FAIL = "gate_fail"
    GATE_PASS = "gate_pass"

    # Subagent lifecycle
    SUBAGENT_SPAWN = "subagent_spawn"
    SUBAGENT_COMPLETE = "subagent_complete"
    SUBAGENT_TIMEOUT = "subagent_timeout"
    SUBAGENT_ERROR = "subagent_error"

    # Escalation
    ESCALATION_RAISED = "escalation_raised"
    ESCALATION_RESOLVED = "escalation_resolved"

    # Workflow
    WORKFLOW_START = "workflow_start"
    WORKFLOW_COMPLETE = "workflow_complete"
    WORKFLOW_FAIL = "workflow_fail"
    CHECKPOINT_SAVED = "checkpoint_saved"
    CHECKPOINT_RESTORED = "checkpoint_restored"

    # Correction loop
    CORRECTION_ITERATION = "correction_iteration"

    # PM
    PM_START = "pm_start"
    PM_COMPLETE = "pm_complete"


# ---------------------------------------------------------------------------
# Event Dataclass
# ---------------------------------------------------------------------------


@dataclass
class LogEvent:
    ts: str  # ISO timestamp
    run_id: str
    event: str
    phase: str | None = None  # Current workflow phase
    agent: str | None = None  # Subagent type if applicable
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ---------------------------------------------------------------------------
# Subagent Input/Output Record
# ---------------------------------------------------------------------------


@dataclass
class SubagentRecord:
    ts: str
    run_id: str
    agent_type: str
    task: str  # Input task description (truncated if large)
    model: str | None
    provider: str
    status: str
    summary: str
    duration_ms: int
    files_changed: list[str] = field(default_factory=list)
    findings_count: dict[str, int] = field(default_factory=dict)
    coverage: float | None = None
    risk_score: int = 0
    test_failures: list[str] = field(default_factory=list)
    error: str | None = None
    raw_output_truncated: str | None = None  # First 2000 chars

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


# ---------------------------------------------------------------------------
# TeamLogger
# ---------------------------------------------------------------------------


class TeamLogger:
    """Structured logger for multi-agent-team workflow.

    Creates per-run log directories with:
    - audit.jsonl: Phase transitions, gate decisions, escalations
    - subagents.jsonl: Per-agent input/output records
    - summary.json: Final run summary (written on completion)
    """

    LOG_VERSION = "1.0"

    def __init__(
        self,
        base_dir: str | Path | None = None,
        team_id: str | None = None,
        task: str | None = None,
    ) -> None:
        if base_dir is None:
            # Default: ~/.multi-agent-team/logs
            base_dir = Path.home() / ".multi-agent-team" / "logs"

        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self.run_id = team_id or self._generate_run_id()
        self.task = task or ""

        # Create run directory
        self.run_dir = self.base_dir / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)

        # Log file paths
        self.audit_path = self.run_dir / "audit.jsonl"
        self.subagents_path = self.run_dir / "subagents.jsonl"

        # Summary path (written on completion)
        self.summary_path = self.run_dir / "summary.json"

        # Track open handles for performance (flush periodically)
        self._audit_handle: Any = None
        self._subagents_handle: Any = None

        # Summary data accumulated during run
        self._summary: dict[str, Any] = {
            "run_id": self.run_id,
            "task": self.task,
            "start_ts": self._iso_now(),
            "end_ts": None,
            "duration_ms": 0,
            "version": self.LOG_VERSION,
            "events_count": 0,
            "phases_completed": [],
            "gate_decisions": [],
            "findings_total": {"critical": 0, "high": 0, "medium": 0, "low": 0},
            "subagents": [],
            "correction_iterations": 0,
            "final_gate": None,
            "escalations": [],
        }

        self._start_time = time.monotonic()
        self._events_count = 0

        # Write workflow start event
        self._emit_audit(EventType.WORKFLOW_START, data={
            "task": self.task[:200] if self.task else None,
            "version": self.LOG_VERSION,
        })

        logger.info(f"TeamLogger initialized for run {self.run_id}")

    # ---------------------------------------------------------------------------
    # Helpers
    # ---------------------------------------------------------------------------

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    @staticmethod
    def _generate_run_id() -> str:
        # Format: run-{timestamp}-{shortuuid}
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        short_id = uuid.uuid4().hex[:6]
        return f"run-{ts}-{short_id}"

    # ---------------------------------------------------------------------------
    # Audit log (phase transitions, gate decisions, escalations)
    # ---------------------------------------------------------------------------

    def _emit_audit(self, event_type: EventType, phase: str | None = None, agent: str | None = None, data: dict[str, Any] | None = None) -> None:
        """Write an audit event to audit.jsonl."""
        event = LogEvent(
            ts=self._iso_now(),
            run_id=self.run_id,
            event=event_type.value,
            phase=phase,
            agent=agent,
            data=data or {},
        )

        try:
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
                f.flush()
        except Exception as exc:
            logger.warning(f"Failed to write audit log: {exc}")

        self._events_count += 1
        self._summary["events_count"] = self._events_count

    def _emit_subagent(self, record: SubagentRecord) -> None:
        """Write a subagent record to subagents.jsonl."""
        try:
            with open(self.subagents_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record.to_dict(), ensure_ascii=False) + "\n")
                f.flush()
        except Exception as exc:
            logger.warning(f"Failed to write subagent log: {exc}")

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    def log_workflow_start(self) -> None:
        self._emit_audit(EventType.WORKFLOW_START)

    def log_workflow_complete(self, gate: str) -> None:
        duration_ms = int((time.monotonic() - self._start_time) * 1000)
        self._summary["end_ts"] = self._iso_now()
        self._summary["duration_ms"] = duration_ms
        self._summary["final_gate"] = gate
        self._emit_audit(
            EventType.WORKFLOW_COMPLETE,
            data={"gate": gate, "duration_ms": duration_ms}
        )
        self._write_summary()

    def log_workflow_fail(self, reason: str) -> None:
        self._summary["end_ts"] = self._iso_now()
        self._summary["final_gate"] = "fail"
        self._emit_audit(EventType.WORKFLOW_FAIL, data={"reason": reason})
        self._write_summary()

    # PM

    def log_pm_start(self) -> None:
        self._emit_audit(EventType.PM_START, phase="pm")

    def log_pm_complete(self, criteria_count: int, raw_output: str | None = None) -> None:
        self._emit_audit(
            EventType.PM_COMPLETE,
            phase="pm",
            data={
                "criteria_count": criteria_count,
                "has_raw_output": raw_output is not None,
            }
        )
        self._summary["criteria_count"] = criteria_count

    # Phase transitions

    def log_phase_start(self, phase: str) -> None:
        self._emit_audit(EventType.PHASE_START, phase=phase)
        if phase not in self._summary["phases_completed"]:
            # Only track phases we've entered (not necessarily completed)
            pass

    def log_phase_end(self, phase: str, next_phase: str | None = None) -> None:
        data: dict[str, Any] = {}
        if next_phase:
            data["next_phase"] = next_phase
        self._emit_audit(EventType.PHASE_END, phase=phase, data=data)
        if phase not in self._summary["phases_completed"]:
            self._summary["phases_completed"].append(phase)

    def log_phase_skip(self, phase: str, reason: str) -> None:
        self._emit_audit(
            EventType.PHASE_SKIP,
            phase=phase,
            data={"reason": reason}
        )

    # Gate decisions

    def log_gate_decision(
        self,
        phase: str,
        decision: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self._emit_audit(
            EventType.GATE_DECISION,
            phase=phase,
            data={
                "decision": decision,
                **(details or {}),
            }
        )
        self._summary["gate_decisions"].append({
            "phase": phase,
            "decision": decision,
            "ts": self._iso_now(),
        })

    def log_gate_pass(self, phase: str, reason: str | None = None) -> None:
        self._emit_audit(
            EventType.GATE_PASS,
            phase=phase,
            data={"reason": reason} if reason else {}
        )

    def log_gate_fail(self, phase: str, reason: str, details: dict[str, Any] | None = None) -> None:
        self._emit_audit(
            EventType.GATE_FAIL,
            phase=phase,
            data={
                "reason": reason,
                **(details or {}),
            }
        )

    # Subagent lifecycle

    def log_subagent_spawn(
        self,
        agent_type: str,
        model: str | None,
        provider: str,
        task_preview: str,
    ) -> None:
        self._emit_audit(
            EventType.SUBAGENT_SPAWN,
            agent=agent_type,
            data={
                "model": model,
                "provider": provider,
                "task_preview": task_preview[:100],
            }
        )

    def log_subagent_complete(
        self,
        agent_type: str,
        status: str,
        summary: str,
        duration_ms: int,
        files_changed: list[str] | None = None,
        findings_count: dict[str, int] | None = None,
        coverage: float | None = None,
        risk_score: int = 0,
        test_failures: list[str] | None = None,
        raw_output_truncated: str | None = None,
        error: str | None = None,
    ) -> None:
        self._emit_audit(
            EventType.SUBAGENT_COMPLETE,
            agent=agent_type,
            data={
                "status": status,
                "summary": summary[:200],
                "duration_ms": duration_ms,
                "files_changed_count": len(files_changed) if files_changed else 0,
            }
        )

        # Write detailed subagent record
        record = SubagentRecord(
            ts=self._iso_now(),
            run_id=self.run_id,
            agent_type=agent_type,
            task="",  # Will be set by caller if needed
            model=None,
            provider="",
            status=status,
            summary=summary,
            duration_ms=duration_ms,
            files_changed=files_changed or [],
            findings_count=findings_count or {},
            coverage=coverage,
            risk_score=risk_score,
            test_failures=test_failures or [],
            raw_output_truncated=raw_output_truncated,
            error=error,
        )
        self._emit_subagent(record)

        # Update summary
        self._summary["subagents"].append(agent_type)

        # Accumulate findings
        if findings_count:
            for sev, count in findings_count.items():
                if sev in self._summary["findings_total"]:
                    self._summary["findings_total"][sev] += count

    def log_subagent_timeout(self, agent_type: str, timeout_seconds: int) -> None:
        self._emit_audit(
            EventType.SUBAGENT_TIMEOUT,
            agent=agent_type,
            data={"timeout_seconds": timeout_seconds}
        )

    def log_subagent_error(self, agent_type: str, error: str) -> None:
        self._emit_audit(
            EventType.SUBAGENT_ERROR,
            agent=agent_type,
            data={"error": error[:200]}
        )

    # Escalation

    def log_escalation_raised(self, escalation: dict[str, Any]) -> None:
        self._emit_audit(EventType.ESCALATION_RAISED, data=escalation)
        self._summary["escalations"].append(escalation)

    def log_escalation_resolved(self, escalation_id: str, resolution: str) -> None:
        self._emit_audit(
            EventType.ESCALATION_RESOLVED,
            data={"escalation_id": escalation_id, "resolution": resolution}
        )
        # Mark as resolved in summary
        for esc in self._summary["escalations"]:
            if esc.get("id") == escalation_id:
                esc["resolved"] = True
                esc["resolution"] = resolution

    # Correction loop

    def log_correction_iteration(self, iteration: int, reason: str) -> None:
        self._emit_audit(
            EventType.CORRECTION_ITERATION,
            data={"iteration": iteration, "reason": reason}
        )
        self._summary["correction_iterations"] = iteration

    # Checkpoint

    def log_checkpoint_saved(self, checkpoint_data: dict[str, Any]) -> None:
        # Write checkpoint file
        checkpoint_path = self.run_dir / "checkpoint.json"
        try:
            with open(checkpoint_path, "w", encoding="utf-8") as f:
                json.dump({
                    "run_id": self.run_id,
                    "ts": self._iso_now(),
                    "data": checkpoint_data,
                }, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning(f"Failed to write checkpoint: {exc}")

        self._emit_audit(EventType.CHECKPOINT_SAVED)

    def log_checkpoint_restored(self, checkpoint_data: dict[str, Any]) -> None:
        self._emit_audit(EventType.CHECKPOINT_RESTORED, data={
            "checkpoint_ts": checkpoint_data.get("ts"),
        })

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------

    def _write_summary(self) -> None:
        """Write summary.json on run completion."""
        try:
            with open(self.summary_path, "w", encoding="utf-8") as f:
                json.dump(self._summary, f, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.warning(f"Failed to write summary: {exc}")

    def get_summary(self) -> dict[str, Any]:
        """Return current summary (does not write file)."""
        return self._summary.copy()

    # ---------------------------------------------------------------------------
    # Checkpoint helpers
    # ---------------------------------------------------------------------------

    @classmethod
    def load_checkpoint(cls, run_id: str, base_dir: str | Path | None = None) -> dict[str, Any] | None:
        """Load checkpoint data for a given run_id."""
        if base_dir is None:
            base_dir = Path.home() / ".multi-agent-team" / "logs"
        checkpoint_path = Path(base_dir) / "runs" / run_id / "checkpoint.json"
        if checkpoint_path.exists():
            try:
                with open(checkpoint_path, encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("data")
            except Exception:
                pass
        return None

    @classmethod
    def list_runs(cls, base_dir: str | Path | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """List recent runs with their summaries."""
        if base_dir is None:
            base_dir = Path.home() / ".multi-agent-team" / "logs"
        runs_dir = Path(base_dir) / "runs"

        if not runs_dir.exists():
            return []

        runs = []
        for run_path in sorted(runs_dir.iterdir(), reverse=True):
            if not run_path.is_dir():
                continue
            summary_path = run_path / "summary.json"
            if summary_path.exists():
                try:
                    with open(summary_path, encoding="utf-8") as f:
                        runs.append(json.load(f))
                except Exception:
                    pass
            if len(runs) >= limit:
                break

        return runs

    # ---------------------------------------------------------------------------
    # Cleanup
    # ---------------------------------------------------------------------------

    def close(self) -> None:
        """Close handles and flush any remaining data."""
        self._write_summary()
        logger.info(f"TeamLogger for run {self.run_id} closed")


# ---------------------------------------------------------------------------
# NullLogger (no-op fallback)
# ---------------------------------------------------------------------------


class NullLogger:
    """No-op logger when logging is disabled."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def log_workflow_start(self) -> None: pass
    def log_workflow_complete(self, gate: str) -> None: pass
    def log_workflow_fail(self, reason: str) -> None: pass
    def log_pm_start(self) -> None: pass
    def log_pm_complete(self, criteria_count: int, raw_output: str | None = None) -> None: pass
    def log_phase_start(self, phase: str) -> None: pass
    def log_phase_end(self, phase: str, next_phase: str | None = None) -> None: pass
    def log_phase_skip(self, phase: str, reason: str) -> None: pass
    def log_gate_decision(self, phase: str, decision: str, details: dict[str, Any] | None = None) -> None: pass
    def log_gate_pass(self, phase: str, reason: str | None = None) -> None: pass
    def log_gate_fail(self, phase: str, reason: str, details: dict[str, Any] | None = None) -> None: pass
    def log_subagent_spawn(self, agent_type: str, model: str | None, provider: str, task_preview: str) -> None: pass
    def log_subagent_complete(self, agent_type: str, status: str, summary: str, duration_ms: int, files_changed: list[str] | None = None, findings_count: dict[str, int] | None = None, coverage: float | None = None, risk_score: int = 0, test_failures: list[str] | None = None, raw_output_truncated: str | None = None, error: str | None = None) -> None: pass
    def log_subagent_timeout(self, agent_type: str, timeout_seconds: int) -> None: pass
    def log_subagent_error(self, agent_type: str, error: str) -> None: pass
    def log_escalation_raised(self, escalation: dict[str, Any]) -> None: pass
    def log_escalation_resolved(self, escalation_id: str, resolution: str) -> None: pass
    def log_correction_iteration(self, iteration: int, reason: str) -> None: pass
    def log_checkpoint_saved(self, checkpoint_data: dict[str, Any]) -> None: pass
    def log_checkpoint_restored(self, checkpoint_data: dict[str, Any]) -> None: pass
    def get_summary(self) -> dict[str, Any]: return {}
    def close(self) -> None: pass

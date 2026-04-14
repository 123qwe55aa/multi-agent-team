#!/usr/bin/env python3
"""
Quick CLI for multi-agent-team orchestrator.

Usage:
    python run.py "实现一个计算器" --model gpt-5.2
    python run.py "实现登录功能" --audit-model gpt-5.3-codex

Interactive mode:
    python run.py --interactive
    > /team extend 2.0
    > /team extend reset
    > run 实现一个计算器
    > exit
"""
import argparse
import json
import math
import sys
from pathlib import Path

# Add project to path
sys.path.insert(0, str(Path(__file__).parent))

from contracts import normalize_request
from orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# Interactive REPL
# ---------------------------------------------------------------------------


class InteractiveREPL:
    """Interactive REPL for team commands."""

    def __init__(self) -> None:
        self.timeout_multipliers: dict[str, float] = {}
        self.orchestrator = Orchestrator()
        self._last_result: dict | None = None

    def _parse_extend(self, args_str: str) -> tuple[str, float] | tuple[None, str]:
        """Parse /team extend <scope> <multiplier>.

        Returns (scope_key, multiplier) on success, or (None, error_msg) on failure.
        """
        parts = args_str.strip().split()
        if len(parts) == 1 and parts[0].lower() == "reset":
            return ("reset", 1.0)
        if len(parts) == 2:
            scope_key = parts[0]
            try:
                multiplier = float(parts[1])
            except ValueError:
                return (None, f"Invalid multiplier: '{parts[1]}' - must be a number")
            return (scope_key, multiplier)
        elif len(parts) == 1:
            try:
                multiplier = float(parts[0])
                # If only one argument and it's a number, apply to all agents
                return ("all", multiplier)
            except ValueError:
                return (None, f"Usage: /team extend <scope> <multiplier> or /team extend <multiplier> (applies to all)")
        return (None, "Usage: /team extend <scope> <multiplier> | /team extend reset")

    def _validate_multiplier(self, multiplier: float) -> str | None:
        """Validate multiplier. Returns error message or None if valid."""
        if not isinstance(multiplier, (int, float)):
            return f"Multiplier must be a number, got {type(multiplier).__name__}"
        if not (multiplier > 0):
            return f"Multiplier must be > 0, got {multiplier}"
        if not math.isfinite(multiplier):
            return f"Multiplier must be finite, got {multiplier}"
        return None

    def _do_extend(self, args_str: str) -> bool:
        """Handle /team extend command. Returns True if should continue REPL."""
        scope_key, multiplier_or_marker = self._parse_extend(args_str)

        if scope_key is None:
            print(f"Error: {multiplier_or_marker}")
            print("Usage: /team extend <scope> <multiplier>  e.g. /team extend coding 2.0")
            print("       /team extend <multiplier>          applies to all agents")
            print("       /team extend reset                  resets all multipliers to 1.0")
            return True

        if scope_key == "reset":
            self.timeout_multipliers = {}
            print("Timeout multipliers reset to default (1.0) for all agents")
            return True

        # Validate multiplier before storing
        err = self._validate_multiplier(multiplier_or_marker)
        if err:
            print(f"Error: {err}")
            return True

        multiplier = multiplier_or_marker
        self.timeout_multipliers[scope_key] = multiplier
        print(f"Timeout multiplier set: {scope_key} = {multiplier}x")
        print(f"  Current multipliers: {self.timeout_multipliers}")
        return True

    def _do_run(self, task: str) -> None:
        """Execute a task with the configured multipliers."""
        if not task.strip():
            print("Error: task cannot be empty")
            return

        request_data = {
            "task": task,
            "config": {
                "mode": "execute",
                "model": "gpt-5.2",
                "coverage_target": 0.8,
                "models": {
                    "pm": "gpt-5.2",
                    "coding": "MiniMax-M2.7",
                    "audit": "gpt-5.4",
                    "testing": "gpt-5.3-codex",
                },
                "providers": {
                    "pm": "codex",
                    "coding": "claude",
                    "audit": "codex",
                    "testing": "claude",
                },
                "enable_logging": True,
                "log_dir": None,
            }
        }

        print(f"Task: {task}")
        print(f"Timeout multipliers: {self.timeout_multipliers}")
        print("-" * 50)

        request = normalize_request(request_data)
        self._last_result = self.orchestrator.run(
            request,
            timeout_multipliers=self.timeout_multipliers if self.timeout_multipliers else None
        )

        print("-" * 50)
        if "run_id" in self._last_result:
            print(f"Run ID: {self._last_result['run_id']}")
            print(f"Log dir: {self._last_result['log_dir']}")
        print(json.dumps(self._last_result, indent=2))

    def run(self) -> None:
        """Run the interactive REPL loop."""
        print("Multi-agent team orchestrator - interactive mode")
        print("Commands:")
        print("  /team extend <scope> <multiplier>  Set timeout multiplier (e.g. /team extend coding 2.0)")
        print("  /team extend <multiplier>          Apply multiplier to all agents")
        print("  /team extend reset                Reset all multipliers to 1.0")
        print("  run <task>                        Execute a task")
        print("  exit                              Exit")
        print()

        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nExiting.")
                break

            if not line:
                continue

            if line.lower() in ("exit", "quit", "q"):
                print("Exiting.")
                break

            if line.startswith("/team extend"):
                args_str = line[len("/team extend"):].strip()
                if not args_str:
                    print("Usage: /team extend <scope> <multiplier> or /team extend reset")
                    continue
                self._do_extend(args_str)
            elif line.startswith("run "):
                task = line[4:].strip()
                self._do_run(task)
            else:
                print(f"Unknown command: {line}")
                print("Available: /team extend <args>, run <task>, exit")


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Run multi-agent-team orchestrator")
    parser.add_argument("task", nargs="?", help="Task description")
    parser.add_argument("--interactive", "-i", action="store_true", help="Interactive REPL mode")
    parser.add_argument("--model", default="gpt-5.2", help="Default model (default: gpt-5.2)")
    parser.add_argument("--pm-model", default="gpt-5.2", help="PM agent model (default: gpt-5.2)")
    parser.add_argument("--coding-model", default="MiniMax-M2.7", help="Coding agent model")
    parser.add_argument("--audit-model", default="gpt-5.4", help="Audit agent model")
    parser.add_argument("--testing-model", default="gpt-5.3-codex", help="Testing agent model (default: gpt-5.3-codex)")
    parser.add_argument("--coverage", type=float, default=0.8, help="Coverage target (default: 0.8)")
    parser.add_argument("--mode", default="execute", choices=["execute", "plan_only"], help="Execution mode")
    parser.add_argument("--log-dir", help="Custom log directory (default: ~/.multi-agent-team/logs)")
    parser.add_argument("--no-log", action="store_true", help="Disable structured logging")
    parser.add_argument("--extend", help="Extend timeout for agent(s): coding=2.0,pm=1.5 or 'all=1.5'")
    args = parser.parse_args()

    # Interactive mode
    if args.interactive or args.task is None:
        InteractiveREPL().run()
        return

    # Build request
    request_data = {
        "task": args.task,
        "config": {
            "mode": args.mode,
            "model": args.model,
            "coverage_target": args.coverage,
            "models": {
                "pm": args.pm_model,
                "coding": args.coding_model,
                "audit": args.audit_model,
                "testing": args.testing_model,
            },
            "providers": {
                "pm": "codex",
                "coding": "claude",
                "audit": "codex",
                "testing": "claude",
            },
            "enable_logging": not args.no_log,
            "log_dir": args.log_dir,
        }
    }

    print(f"Task: {args.task}")
    print(f"Models: pm={args.pm_model}, coding={args.coding_model}, audit={args.audit_model}, testing={args.testing_model}")
    if not args.no_log:
        log_path = args.log_dir or "~/.multi-agent-team/logs"
        print(f"Logging: enabled ({log_path})")
    else:
        print("Logging: disabled")
    print("-" * 50)

    request = normalize_request(request_data)
    orchestrator = Orchestrator()

    # Parse --extend argument (e.g., "coding=2.0,pm=1.5" or "all=1.5")
    timeout_multipliers: dict[str, float] | None = None
    if args.extend:
        timeout_multipliers = {}
        for part in args.extend.split(","):
            if "=" in part:
                key, value = part.split("=", 1)
                timeout_multipliers[key.strip()] = float(value.strip())
        print(f"Timeout multipliers: {timeout_multipliers}")

    result = orchestrator.run(request, timeout_multipliers=timeout_multipliers)

    print("-" * 50)
    if "run_id" in result:
        print(f"Run ID: {result['run_id']}")
        print(f"Log dir: {result['log_dir']}")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

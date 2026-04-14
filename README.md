# Multi-Agent Team MCP Skeleton

This repository is now intentionally narrowed to a single goal: provide a
stable Claude Code MCP tool interface that later development can build on.

## Current Scope

- Exposes one tool: `multi_agent_team`
- Validates and normalizes task payloads
- Returns a standard orchestration response shape
- Supports `plan_only` today
- Leaves `execute` mode as an explicit stub

This version does not yet spawn subagents, run tests, or persist checkpoints.

## Files

- `server.py`: minimal stdio JSON-RPC MCP server
- `contracts.py`: request validation and normalization
- `orchestrator.py`: planning-only orchestration facade
- `tests/`: contract and orchestrator tests

## Tool Input

```json
{
  "task": "Implement login flow",
  "context": {
    "goal": "Ship MVP auth",
    "files": ["app/auth.py"],
    "constraints": ["Keep backward compatibility"]
  },
  "config": {
    "mode": "plan_only",
    "workflow": "auto",
    "coverage_target": 0.8
  }
}
```

## Tool Output

The tool returns MCP text content containing a JSON object with this shape:

```json
{
  "summary": "Accepted task and created a planning stub",
  "gateDecision": "not_run",
  "completedPhases": ["intake", "planning"],
  "findings": {
    "critical": 0,
    "high": 0,
    "medium": 0,
    "low": 0
  },
  "escalation": null,
  "nextAction": "Implement the coding, audit, and testing runners",
  "plan": {
    "workflow": "auto",
    "mode": "plan_only",
    "coverageTarget": 0.8,
    "task": "Implement login flow",
    "goal": "Ship MVP auth",
    "files": ["app/auth.py"],
    "constraints": ["Keep backward compatibility"]
  }
}
```

## Claude Code Registration

Add the server to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "multi-agent-team": {
      "command": "python3",
      "args": ["/Users/toby/multi-agent-team/server.py"]
    }
  }
}
```

## Run Tests

```bash
python3 -m unittest discover -s tests
```

## Recommended Next Steps

1. Add a real `execute` pipeline behind `Orchestrator.run`.
2. Split runner interfaces into coding, audit, and testing modules.
3. Add session state and recovery only after execution exists.


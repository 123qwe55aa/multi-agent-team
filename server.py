from __future__ import annotations

import json
import logging
import sys
from typing import Any

from contracts import RequestValidationError, normalize_request
from orchestrator import Orchestrator


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)


SERVER_INFO = {
    "name": "multi-agent-team",
    "version": "0.1.0",
}

TOOL_DEFINITION = {
    "name": "multi_agent_team",
    "description": (
        "Spawn an autonomous team (Team Leader + Coding/Audit/Testing) for task execution. "
        "Team Leader makes gate decisions independently, notifies Main Agent on escalation."
    ),
    "inputSchema": {
        "type": "object",
        "required": ["task"],
        "properties": {
            "task": {
                "type": "string",
                "description": "Task description for the orchestrator.",
            },
            "context": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "files": {"type": "array", "items": {"type": "string"}},
                    "constraints": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
            "config": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "enum": ["plan_only", "execute"],
                        "default": "execute",
                    },
                    "workflow": {
                        "type": "string",
                        "enum": ["auto", "sequential", "parallel"],
                        "default": "auto",
                    },
                    "coverage_target": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "default": 0.8,
                    },
                    "audit_block_threshold": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                        "default": "high",
                    },
                    "model": {
                        "type": "string",
                        "description": "Default model (e.g., 'gpt-5.2' for Codex, 'MiniMax-M2.7' for Claude)",
                    },
                    "provider": {
                        "type": "string",
                        "enum": ["codex", "claude"],
                        "default": "codex",
                    },
                    "models": {
                        "type": "object",
                        "description": "Per-agent model selection, e.g., {'coding': 'MiniMax-M2.7', 'audit': 'gpt-5.4', 'testing': 'MiniMax-M2.7'}",
                        "additionalProperties": {"type": "string"},
                    },
                    "providers": {
                        "type": "object",
                        "description": "Per-agent provider selection, e.g., {'coding': 'claude', 'audit': 'codex', 'testing': 'claude'}",
                        "additionalProperties": {"type": "string", "enum": ["codex", "claude"]},
                    },
                },
                "additionalProperties": False,
            },
        },
        "additionalProperties": False,
    },
}

TOOL_DEFINITIONS = [
    TOOL_DEFINITION,
    {
        "name": "multi_agent_team_status",
        "description": "Get current status of a multi-agent team session",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID to check",
                },
            },
            "required": ["session_id"],
        },
    },
    {
        "name": "multi_agent_team_resume",
        "description": "Resume a paused team session with Main Agent instruction",
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Session ID to resume",
                },
                "instruction": {
                    "type": "string",
                    "description": "Instruction from Main Agent or user",
                },
            },
            "required": ["session_id"],
        },
    },
]


class JsonRpcError(Exception):
    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def main() -> int:
    orchestrator = Orchestrator()

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
            response = handle_message(request, orchestrator)
        except JsonRpcError as exc:
            response = build_error_response(
                request_id=_safe_request_id(locals().get("request")),
                code=exc.code,
                message=exc.message,
                data=exc.data,
            )
        except Exception as exc:  # pragma: no cover - safety net
            logging.exception("Unhandled server error")
            response = build_error_response(
                request_id=_safe_request_id(locals().get("request")),
                code=-32603,
                message="Internal error",
                data={"detail": str(exc)},
            )

        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()

    return 0


def handle_message(message: dict[str, Any], orchestrator: Orchestrator) -> dict[str, Any] | None:
    if not isinstance(message, dict):
        raise JsonRpcError(-32600, "Invalid Request")

    method = message.get("method")
    params = message.get("params", {})
    request_id = message.get("id")

    if method == "initialize":
        return build_response(
            request_id,
            {
                "protocolVersion": "2024-11-05",
                "serverInfo": SERVER_INFO,
                "capabilities": {"tools": {}},
            },
        )

    if method == "notifications/initialized":
        return None

    if method == "ping":
        return build_response(request_id, {})

    if method == "tools/list":
        return build_response(request_id, {"tools": TOOL_DEFINITIONS})

    if method == "tools/call":
        return build_response(request_id, handle_tool_call(params, orchestrator))

    raise JsonRpcError(-32601, f"Method not found: {method}")


def handle_tool_call(params: dict[str, Any], orchestrator: Orchestrator) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise JsonRpcError(-32602, "Invalid params")

    name = params.get("name")
    arguments = params.get("arguments", {})

    if name == "multi_agent_team":
        return handle_multi_agent_team(arguments, orchestrator)
    elif name == "multi_agent_team_status":
        return handle_status(arguments)
    elif name == "multi_agent_team_resume":
        return handle_resume(arguments)
    else:
        raise JsonRpcError(-32602, f"Unknown tool: {name}")


def handle_multi_agent_team(arguments: dict[str, Any], orchestrator: Orchestrator) -> dict[str, Any]:
    """Handle multi_agent_team tool call."""
    try:
        request = normalize_request(arguments)
    except RequestValidationError as exc:
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "summary": "Request validation failed.",
                            "gateDecision": "fail",
                            "completedPhases": ["intake"],
                            "findings": {
                                "critical": 0,
                                "high": 1,
                                "medium": 0,
                                "low": 0,
                            },
                            "escalation": {
                                "type": "invalid_input",
                                "severity": "high",
                                "actionTaken": str(exc),
                                "notified": True,
                            },
                            "nextAction": "Fix the tool arguments and retry.",
                        }
                    ),
                }
            ],
            "isError": True,
        }

    result = orchestrator.run(request)
    return {
        "content": [{"type": "text", "text": json.dumps(result)}],
        "isError": False,
    }


# In-memory session store (for stdio mode)
_sessions: dict[str, dict[str, Any]] = {}


def handle_status(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle multi_agent_team_status tool call."""
    session_id = arguments.get("session_id")
    if not session_id:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": "session_id required"})}],
            "isError": True,
        }

    if session_id not in _sessions:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": f"Session {session_id} not found"})}],
            "isError": True,
        }

    return {
        "content": [{"type": "text", "text": json.dumps(_sessions[session_id])}],
        "isError": False,
    }


def handle_resume(arguments: dict[str, Any]) -> dict[str, Any]:
    """Handle multi_agent_team_resume tool call."""
    session_id = arguments.get("session_id")
    instruction = arguments.get("instruction", "")

    if not session_id:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": "session_id required"})}],
            "isError": True,
        }

    if session_id not in _sessions:
        return {
            "content": [{"type": "text", "text": json.dumps({"error": f"Session {session_id} not found"})}],
            "isError": True,
        }

    # TODO: Resume the session with instruction
    # For now, just return acknowledgment
    return {
        "content": [{"type": "text", "text": json.dumps({
            "summary": f"Session {session_id} resumed with instruction",
            "instruction_received": instruction,
            "nextAction": "Implement resume logic in orchestrator"
        })}],
        "isError": False,
    }


def build_response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def build_error_response(
    request_id: Any, code: int, message: str, data: Any | None = None
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _safe_request_id(request: Any) -> Any:
    if isinstance(request, dict):
        return request.get("id")
    return None


if __name__ == "__main__":
    raise SystemExit(main())

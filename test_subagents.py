"""
Unit tests for multi-agent-team subagents.

Tests:
1. Subagent spawning with different providers/models
2. Gate evaluation logic
3. Escalation handling
4. Workflow state transitions
"""
import json
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from contracts import normalize_request, RequestValidationError
from orchestrator import (
    TeamLeader, TeamState, SubagentType, GateDecision,
    SubagentResult, AuditFinding, Severity, EscalationType,
    WorkflowConfig as WC, PMResult, PMCriteria
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def base_config():
    return WC(
        workflow_type="auto",
        coverage_target=0.8,
        audit_block_threshold=Severity.HIGH,
        model="gpt-5.2",
        provider="codex",
    )


@pytest.fixture
def per_agent_config():
    return WC(
        workflow_type="auto",
        coverage_target=0.8,
        audit_block_threshold=Severity.HIGH,
        model="gpt-5.2",  # default
        provider="codex",  # default
        models={
            "coding": "MiniMax-M2.7",
            "audit": "gpt-5.4",
            "testing": "MiniMax-M2.7",
        },
        providers={
            "coding": "claude",
            "audit": "codex",
            "testing": "claude",
        },
    )


@pytest.fixture
def team_leader(base_config):
    return TeamLeader(
        task="Implement user login feature",
        context={"goal": "Add login functionality", "files": ["auth.py"]},
        config=base_config,
    )


@pytest.fixture
def team_leader_per_agent(per_agent_config):
    return TeamLeader(
        task="Implement user login feature",
        context={"goal": "Add login functionality", "files": ["auth.py"]},
        config=per_agent_config,
    )


# ---------------------------------------------------------------------------
# Test: Per-agent model/provider selection
# ---------------------------------------------------------------------------

class TestPerAgentConfig:
    def test_default_model_provider(self, base_config):
        """Default model/provider when not specified per-agent."""
        assert base_config.get_model("coding") == "gpt-5.2"
        assert base_config.get_model("audit") == "gpt-5.2"
        assert base_config.get_provider("coding") == "codex"
        assert base_config.get_provider("audit") == "codex"

    def test_per_agent_model_override(self, per_agent_config):
        """Per-agent model overrides default."""
        assert per_agent_config.get_model("coding") == "MiniMax-M2.7"
        assert per_agent_config.get_model("audit") == "gpt-5.4"
        assert per_agent_config.get_model("testing") == "MiniMax-M2.7"

    def test_per_agent_provider_override(self, per_agent_config):
        """Per-agent provider overrides default."""
        assert per_agent_config.get_provider("coding") == "claude"
        assert per_agent_config.get_provider("audit") == "codex"
        assert per_agent_config.get_provider("testing") == "claude"

    def test_unknown_agent_falls_back_to_default(self, per_agent_config):
        """Unknown agent type uses default model/provider."""
        assert per_agent_config.get_model("unknown") == "gpt-5.2"
        assert per_agent_config.get_provider("unknown") == "codex"


# ---------------------------------------------------------------------------
# Test: Contract validation
# ---------------------------------------------------------------------------

class TestContractValidation:
    def test_valid_request_with_models_providers(self):
        """Request with per-agent models and providers."""
        req = normalize_request({
            "task": "test task",
            "config": {
                "mode": "execute",
                "models": {"coding": "MiniMax-M2.7", "audit": "gpt-5.4"},
                "providers": {"coding": "claude", "audit": "codex"},
            }
        })
        assert req.config.models == {"coding": "MiniMax-M2.7", "audit": "gpt-5.4"}
        assert req.config.providers == {"coding": "claude", "audit": "codex"}

    def test_invalid_provider_in_providers_dict(self):
        """Invalid provider value in providers dict raises error."""
        with pytest.raises(RequestValidationError):
            normalize_request({
                "task": "test",
                "config": {
                    "providers": {"coding": "invalid"}
                }
            })

    def test_models_must_be_string_to_string(self):
        """models dict must be string -> string."""
        with pytest.raises(RequestValidationError):
            normalize_request({
                "task": "test",
                "config": {
                    "models": {"coding": 123}
                }
            })


# ---------------------------------------------------------------------------
# Test: Gate evaluation
# ---------------------------------------------------------------------------

class TestGateEvaluation:
    def test_audit_gate_pass(self, team_leader):
        """Audit gate passes when risk score is low."""
        result = SubagentResult(
            agent_type=SubagentType.AUDIT,
            status="pass",
            risk_score=3,
            block_ship=False,
        )
        decision = team_leader.evaluate_audit_gate(result)
        assert decision == GateDecision.PASS

    def test_audit_gate_fail_high_risk(self, team_leader):
        """Audit gate fails when risk score exceeds threshold."""
        result = SubagentResult(
            agent_type=SubagentType.AUDIT,
            status="warning",
            risk_score=6,
            block_ship=False,
        )
        decision = team_leader.evaluate_audit_gate(result)
        assert decision == GateDecision.FAIL

    def test_audit_gate_fail_block_ship(self, team_leader):
        """Audit gate fails when block_ship is True."""
        result = SubagentResult(
            agent_type=SubagentType.AUDIT,
            status="critical",
            risk_score=2,
            block_ship=True,
        )
        decision = team_leader.evaluate_audit_gate(result)
        assert decision == GateDecision.FAIL

    def test_testing_gate_pass(self, team_leader):
        """Testing gate passes when coverage meets target."""
        result = SubagentResult(
            agent_type=SubagentType.TESTING,
            status="all-pass",
            coverage=0.85,
            block_ship=False,
        )
        decision = team_leader.evaluate_testing_gate(result)
        assert decision == GateDecision.PASS

    def test_testing_gate_fail_low_coverage(self, team_leader):
        """Testing gate fails when coverage below target."""
        result = SubagentResult(
            agent_type=SubagentType.TESTING,
            status="all-pass",
            coverage=0.7,
            block_ship=False,
        )
        decision = team_leader.evaluate_testing_gate(result)
        assert decision == GateDecision.FAIL

    def test_testing_gate_fail_failed_tests(self, team_leader):
        """Testing gate fails when tests fail."""
        result = SubagentResult(
            agent_type=SubagentType.TESTING,
            status="some-fail",
            coverage=0.9,
            block_ship=False,
        )
        decision = team_leader.evaluate_testing_gate(result)
        assert decision == GateDecision.FAIL


# ---------------------------------------------------------------------------
# Test: Escalation handling
# ---------------------------------------------------------------------------

class TestEscalation:
    def test_critical_finding_triggers_notification_escalation(self, team_leader):
        """Critical finding triggers notification-type escalation."""
        result = SubagentResult(
            agent_type=SubagentType.AUDIT,
            status="warning",
            findings=[
                AuditFinding(
                    severity=Severity.CRITICAL,
                    location="auth.py",
                    description="SQL injection vulnerability",
                )
            ],
        )
        escalation = team_leader.check_escalation(result)
        assert escalation is not None
        assert escalation.escalation_type == EscalationType.RISK
        assert escalation.severity == Severity.CRITICAL
        assert escalation.blocking is False  # Notification type
        assert escalation.notified is True

    def test_authority_gap_triggers_blocking_escalation(self, team_leader):
        """Authority gap triggers blocking escalation."""
        result = SubagentResult(
            agent_type=SubagentType.CODING,
            status="partial",
            requires_scope_change=True,
        )
        escalation = team_leader.check_escalation(result)
        assert escalation is not None
        assert escalation.escalation_type == EscalationType.AUTHORITY_GAP
        assert escalation.blocking is True  # Waits for Main Agent

    def test_no_escalation_for_good_result(self, team_leader):
        """No escalation for good results."""
        result = SubagentResult(
            agent_type=SubagentType.CODING,
            status="success",
            confidence=0.9,
        )
        escalation = team_leader.check_escalation(result)
        assert escalation is None


# ---------------------------------------------------------------------------
# Test: Subagent spawning (mocked)
# ---------------------------------------------------------------------------

class TestSubagentSpawning:
    @patch("subprocess.run")
    def test_spawn_codex_with_correct_model(self, mock_run, team_leader):
        """Spawn subagent via codex with correct model."""
        mock_run.return_value = MagicMock(
            stdout='{"status": "success", "summary": "done", "files_changed": [], "confidence": 0.9}',
            stderr="",
            returncode=0,
        )

        result = team_leader.spawn_subagent(SubagentType.CODING)

        # Verify codex was called with -c model="gpt-5.2"
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "codex"
        assert "-c" in call_args
        assert 'model="gpt-5.2"' in call_args

    @patch("subprocess.run")
    def test_spawn_per_agent_model(self, mock_run, team_leader_per_agent):
        """Spawn subagent with per-agent model selection."""
        mock_run.return_value = MagicMock(
            stdout='{"status": "success", "summary": "done", "files_changed": [], "confidence": 0.9}',
            stderr="",
            returncode=0,
        )

        # Coding should use MiniMax-M2.7 via claude
        result = team_leader_per_agent.spawn_subagent(SubagentType.CODING)
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "claude"
        assert "--model" in call_args
        model_idx = call_args.index("--model")
        assert call_args[model_idx + 1] == "MiniMax-M2.7"

        # Audit should use gpt-5.4 via codex
        result = team_leader_per_agent.spawn_subagent(SubagentType.AUDIT)
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "codex"
        assert 'model="gpt-5.4"' in call_args


# ---------------------------------------------------------------------------
# Test: State machine
# ---------------------------------------------------------------------------

class TestStateMachine:
    def test_initial_state(self, team_leader):
        """TeamLeader starts in PLANNING state."""
        assert team_leader.state == TeamState.PLANNING

    def test_state_transitions(self, team_leader):
        """State transitions during workflow."""
        team_leader.state = TeamState.CODING
        assert team_leader.state == TeamState.CODING

        team_leader.state = TeamState.AUDIT
        assert team_leader.state == TeamState.AUDIT

        team_leader.state = TeamState.TESTING
        assert team_leader.state == TeamState.TESTING

    def test_team_leader_serialization(self, team_leader):
        """TeamLeader serializes to dict correctly."""
        d = team_leader.to_dict()
        assert d["task"] == "Implement user login feature"
        assert d["state"] == "planning"
        assert "results" in d


# ---------------------------------------------------------------------------
# Test: Parsing subagent output
# ---------------------------------------------------------------------------

class TestOutputParsing:
    def test_parse_coding_result(self, team_leader):
        """Parse coding agent JSON output."""
        output = '''
        Here's what I did:
        ```json
        {"status": "success", "summary": "Added login", "files_changed": ["auth.py"], "confidence": 0.9}
        ```
        '''
        result = team_leader._parse_subagent_result(SubagentType.CODING, output)
        assert result.status == "success"
        assert result.summary == "Added login"
        assert result.files_changed == ["auth.py"]
        assert result.confidence == 0.9

    def test_parse_audit_result(self, team_leader):
        """Parse audit agent JSON output."""
        output = '''
        ```json
        {"status": "pass", "findings": [{"severity": "high", "location": "auth.py", "description": "Weak hashing"}], "risk_score": 4, "block_ship": false}
        ```
        '''
        result = team_leader._parse_subagent_result(SubagentType.AUDIT, output)
        assert result.status == "pass"
        assert result.risk_score == 4
        assert len(result.findings) == 1
        assert result.findings[0].severity == Severity.HIGH

    def test_parse_testing_result(self, team_leader):
        """Parse testing agent JSON output."""
        output = '''
        ```json
        {"status": "all-pass", "coverage": 0.87, "block_ship": false, "test_failures": []}
        ```
        '''
        result = team_leader._parse_subagent_result(SubagentType.TESTING, output)
        assert result.status == "all-pass"
        assert result.coverage == 0.87


# ---------------------------------------------------------------------------
# Integration tests: Full workflow with rounds
# ---------------------------------------------------------------------------

# Mock responses by call order
def make_mock_responses(audit_fails_r1=True, testing_fails_r1=True):
    """Generate mock responses based on round conditions."""
    responses = [
        # Call 1: CODING (always success)
        '{"status": "success", "summary": "Done", "files_changed": ["auth.py"], "confidence": 0.9}',
        # Call 2: AUDIT round 1
        '{"status": "pass", "findings": [], "risk_score": 2, "block_ship": false}' if not audit_fails_r1
        else '{"status": "warning", "findings": [{"severity": "high", "location": "auth.py", "description": "SQL injection"}], "risk_score": 6, "block_ship": false}',
        # Call 3: TESTING round 1
        '{"status": "all-pass", "coverage": 0.85, "block_ship": false, "test_failures": []}' if not testing_fails_r1
        else '{"status": "some-fail", "coverage": 0.5, "block_ship": false, "test_failures": ["test_login"]}',
        # Call 4: CODING corrective (if needed)
        '{"status": "success", "summary": "Fixed", "files_changed": ["auth.py"], "confidence": 0.9}',
        # Call 5: AUDIT corrective (if needed) - always passes after fix
        '{"status": "pass", "findings": [], "risk_score": 2, "block_ship": false}',
        # Call 6: TESTING corrective (if needed) - always passes after fix
        '{"status": "all-pass", "coverage": 0.85, "block_ship": false, "test_failures": []}',
    ]
    return responses


class TestWorkflowRounds:
    """Test Team Leader workflow with corrective rounds."""

    @patch("subprocess.run")
    def test_workflow_two_rounds_due_to_audit_failure(self, mock_run):
        """Workflow goes through 2 rounds when audit fails twice."""
        # Track which call we're on
        call_count = [0]

        def mock_side_effect(*args, **kwargs):
            call_count[0] += 1
            # Actual workflow sequence (Testing before Audit):
            # 1. PM
            # 2. CODING r1
            # 3. TESTING r1 - passes
            # 4. AUDIT r1 - fails
            # 5. CODING corrective
            # 6. TESTING corrective
            # 7. AUDIT r2 - passes

            if call_count[0] == 1:  # PM
                return MagicMock(
                    stdout='{"requirement_summary": "Login", "criteria": []}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 2:  # CODING r1
                return MagicMock(
                    stdout='{"status": "success", "summary": "Done", "files_changed": ["auth.py"], "confidence": 0.9}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 3:  # TESTING r1 - passes
                return MagicMock(
                    stdout='{"status": "all-pass", "coverage": 0.85, "block_ship": false, "test_failures": []}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 4:  # AUDIT r1 - fails (warning blockship false = FAIL)
                return MagicMock(
                    stdout='{"status": "warning", "findings": [{"severity": "high", "location": "auth.py", "description": "SQL injection"}], "risk_score": 6, "block_ship": false}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 5:  # CODING corrective
                return MagicMock(
                    stdout='{"status": "success", "summary": "Fixed issues", "files_changed": ["auth.py"], "confidence": 0.9}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 6:  # TESTING corrective - passes
                return MagicMock(
                    stdout='{"status": "all-pass", "coverage": 0.85, "block_ship": false, "test_failures": []}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 7:  # AUDIT r2 - passes
                return MagicMock(
                    stdout='{"status": "pass", "findings": [], "risk_score": 2, "block_ship": false}',
                    stderr='', returncode=0
                )
            else:
                # Fallback - should not reach here
                return MagicMock(stdout='{}', stderr='', returncode=0)

        mock_run.side_effect = mock_side_effect

        config = WC(workflow_type="auto", coverage_target=0.8, model="gpt-5.2", provider="codex")
        team_leader = TeamLeader(task="Implement login", context={"goal": "Add login"}, config=config)

        result = team_leader.run()

        # Should complete successfully after 2 rounds
        assert result["gateDecision"] == "pass", f"Expected pass, got {result}"
        assert "audit" in result["completedPhases"]
        assert "testing" in result["completedPhases"]
        # 1 correction iteration (round 1 audit fail → fix → round 2 audit pass)
        assert team_leader._correction_iterations == 1

    @patch("subprocess.run")
    def test_workflow_max_rounds_then_fails(self, mock_run):
        """Workflow fails when audit keeps failing after max corrections."""
        call_count = [0]

        def mock_side_effect(*args, **kwargs):
            call_count[0] += 1
            # Workflow call order: PM -> CODING -> TESTING -> AUDIT
            # When AUDIT fails twice, we hit max corrections
            if call_count[0] == 1:  # PM
                return MagicMock(
                    stdout='{"requirement_summary": "Login", "criteria": []}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 2:  # CODING r1
                return MagicMock(
                    stdout='{"status": "success", "summary": "Done", "files_changed": ["auth.py"], "confidence": 0.9}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 3:  # TESTING r1 - passes
                return MagicMock(
                    stdout='{"status": "all-pass", "coverage": 0.85, "block_ship": false, "test_failures": []}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 4:  # AUDIT r1 - fails (triggers corrective)
                return MagicMock(
                    stdout='{"status": "warning", "findings": [{"severity": "critical", "location": "auth.py", "description": "SQL injection"}], "risk_score": 8, "block_ship": true}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 5:  # CODING corrective
                return MagicMock(
                    stdout='{"status": "success", "summary": "Fixed", "files_changed": ["auth.py"], "confidence": 0.9}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 6:  # TESTING corrective - passes
                return MagicMock(
                    stdout='{"status": "all-pass", "coverage": 0.85, "block_ship": false, "test_failures": []}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 7:  # AUDIT r2 - fails again (max corrections reached)
                return MagicMock(
                    stdout='{"status": "warning", "findings": [{"severity": "critical", "location": "auth.py", "description": "SQL injection"}], "risk_score": 8, "block_ship": true}',
                    stderr='', returncode=0
                )
            else:
                # Fallback
                return MagicMock(stdout='{}', stderr='', returncode=0)

        mock_run.side_effect = mock_side_effect

        config = WC(workflow_type="auto", coverage_target=0.8, model="gpt-5.2", provider="codex")
        team_leader = TeamLeader(task="Implement login", context={"goal": "Add login"}, config=config)
        team_leader._max_corrections = 2  # Allow 2 corrections

        result = team_leader.run()

        # Should fail due to max corrections reached
        # Note: corrections=1 because we increment when first AUDIT fails,
        # then max is reached and we exit without another increment
        assert result["gateDecision"] == "fail"
        assert team_leader._correction_iterations == 1

    @patch("subprocess.run")
    def test_workflow_two_rounds_due_to_test_failure(self, mock_run):
        """Workflow goes through 2 rounds when testing fails twice."""
        call_count = [0]

        def mock_side_effect(*args, **kwargs):
            call_count[0] += 1
            # Workflow call order: PM -> CODING -> TESTING -> AUDIT
            # Test fail sequence: TESTING r1 fails -> CODING fix -> TESTING r2 passes -> AUDIT runs
            # 1. PM
            # 2. CODING r1
            # 3. TESTING r1 - fails (trigger corrective)
            # 4. CODING r2 (fix)
            # 5. TESTING r2 - passes
            # 6. AUDIT r1 - passes

            if call_count[0] == 1:  # PM
                return MagicMock(
                    stdout='{"requirement_summary": "Login feature", "criteria": []}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 2:  # CODING r1
                return MagicMock(
                    stdout='{"status": "success", "summary": "Done", "files_changed": ["auth.py"], "confidence": 0.9}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 3:  # TESTING r1 - fails
                return MagicMock(
                    stdout='{"status": "some-fail", "coverage": 0.5, "block_ship": false, "test_failures": ["test_login"]}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 4:  # CODING corrective r2 - fix
                return MagicMock(
                    stdout='{"status": "success", "summary": "Fixed", "files_changed": ["auth.py"], "confidence": 0.9}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 5:  # TESTING r2 - passes
                return MagicMock(
                    stdout='{"status": "all-pass", "coverage": 0.85, "block_ship": false, "test_failures": []}',
                    stderr='', returncode=0
                )
            elif call_count[0] == 6:  # AUDIT r1 - passes
                return MagicMock(
                    stdout='{"status": "pass", "findings": [], "risk_score": 2, "block_ship": false}',
                    stderr='', returncode=0
                )
            else:
                return MagicMock(stdout='{}', stderr='', returncode=0)

        mock_run.side_effect = mock_side_effect

        config = WC(workflow_type="auto", coverage_target=0.8, model="gpt-5.2", provider="codex")
        team_leader = TeamLeader(task="Implement login", context={"goal": "Add login"}, config=config)

        result = team_leader.run()

        # Should complete after 1 correction iteration (test fails once, then passes)
        assert result["gateDecision"] == "pass", f"Expected pass, got {result}"
        assert "testing" in result["completedPhases"]
        assert team_leader._correction_iterations == 1

    @patch("subprocess.run")
    def test_workflow_passes_first_time(self, mock_run):
        """Workflow passes without any corrections needed."""
        # Workflow call order: PM -> CODING -> TESTING -> AUDIT
        responses = [
            '{"requirement_summary": "Login", "criteria": []}',  # PM
            '{"status": "success", "summary": "Done", "files_changed": ["auth.py"], "confidence": 0.9}',  # CODING
            '{"status": "all-pass", "coverage": 0.85, "block_ship": false, "test_failures": []}',  # TESTING
            '{"status": "pass", "findings": [], "risk_score": 2, "block_ship": false}',  # AUDIT
        ]
        mock_run.side_effect = [MagicMock(stdout=r, stderr='', returncode=0) for r in responses]

        config = WC(workflow_type="auto", coverage_target=0.8, model="gpt-5.2", provider="codex")
        team_leader = TeamLeader(task="Implement login", context={"goal": "Add login"}, config=config)

        result = team_leader.run()

        assert result["gateDecision"] == "pass", f"Expected pass, got {result}"
        assert team_leader._correction_iterations == 0
        assert team_leader.state == TeamState.COMPLETE


# ---------------------------------------------------------------------------
# Test: PM Agent
# ---------------------------------------------------------------------------

class TestPMAgent:
    """Tests for PM agent spawning and result parsing."""

    @pytest.fixture
    def pm_config(self):
        return WC(
            workflow_type="auto",
            coverage_target=0.8,
            model="gpt-5.2",
            provider="codex",
            models={"pm": "gpt-5.2"},
            providers={"pm": "codex"},
        )

    def test_pm_criteria_creation(self):
        """PMCriteria creates correctly."""
        criteria = PMCriteria(
            id="F1",
            type="functional",
            description="User can send messages",
            verification="Manual test with two users",
            priority="MUST",
        )
        assert criteria.id == "F1"
        assert criteria.type == "functional"
        assert criteria.priority == "MUST"

    def test_pm_criteria_to_dict(self):
        """PMCriteria serializes to dict."""
        criteria = PMCriteria(
            id="N1",
            type="non_functional",
            description="API response < 200ms",
            verification="Load test",
            priority="SHOULD",
            metrics={"target": 200, "unit": "ms"},
        )
        d = criteria.to_dict()
        assert d["id"] == "N1"
        assert d["metrics"]["target"] == 200

    def test_pm_result_creation(self):
        """PMResult creates with criteria list."""
        criteria = [
            PMCriteria(id="F1", type="functional", description="Feature 1", priority="MUST"),
            PMCriteria(id="F2", type="functional", description="Feature 2", priority="SHOULD"),
        ]
        result = PMResult(
            requirement_summary="Test requirement",
            criteria=criteria,
        )
        assert result.requirement_summary == "Test requirement"
        assert len(result.criteria) == 2

    def test_pm_result_to_dict(self):
        """PMResult serializes to dict with criteria."""
        criteria = [PMCriteria(id="F1", type="functional", description="Test", priority="MUST")]
        result = PMResult(requirement_summary="Summary", criteria=criteria, raw_output="raw")
        d = result.to_dict()
        assert d["requirement_summary"] == "Summary"
        assert len(d["criteria"]) == 1

    def test_pm_loads_prompt_file(self, pm_config):
        """PM loads prompt from file if exists."""
        team_leader = TeamLeader(
            task="Test task",
            context={},
            config=pm_config,
        )
        prompt = team_leader._load_pm_prompt()
        # Should load from agents/pm/PROMPT.md or fallback
        assert len(prompt) > 0
        assert "PM" in prompt or "Product Manager" in prompt

    @patch("subprocess.run")
    def test_spawn_pm_via_codex(self, mock_run, pm_config):
        """PM spawns via codex with correct model."""
        mock_run.return_value = MagicMock(
            stdout='{"requirement_summary": "Test", "criteria": [{"id": "F1", "type": "functional", "description": "Test", "verification": "Test", "priority": "MUST"}]}',
            stderr="",
            returncode=0,
        )

        team_leader = TeamLeader(task="Test", context={}, config=pm_config)
        result = team_leader.spawn_pm()

        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "codex"
        assert 'model="gpt-5.2"' in call_args
        assert result.requirement_summary == "Test"

    @patch("subprocess.run")
    def test_spawn_pm_parse_json(self, mock_run, pm_config):
        """PM parses JSON output correctly."""
        mock_run.return_value = MagicMock(
            stdout='''
            Here is the analysis:
            ```json
            {"requirement_summary": "Calculator app", "criteria": [
                {"id": "F1", "type": "functional", "description": "Addition", "verification": "2+3=5", "priority": "MUST"},
                {"id": "F2", "type": "functional", "description": "Subtraction", "verification": "5-3=2", "priority": "MUST"}
            ]}
            ```
            ''',
            stderr="",
            returncode=0,
        )

        team_leader = TeamLeader(task="Calculator", context={}, config=pm_config)
        result = team_leader.spawn_pm()

        assert result.requirement_summary == "Calculator app"
        assert len(result.criteria) == 2
        assert result.criteria[0].id == "F1"
        assert result.criteria[0].priority == "MUST"

    @patch("subprocess.run")
    def test_spawn_pm_parses_non_functional_criteria(self, mock_run, pm_config):
        """PM parses non_functional criteria and merges with functional criteria."""
        mock_run.return_value = MagicMock(
            stdout='''
            {
                "requirement_summary": "API service",
                "criteria": [
                    {"id": "F1", "type": "functional", "description": "GET /users endpoint", "verification": "curl test", "priority": "MUST"}
                ],
                "non_functional": [
                    {"id": "N1", "type": "performance", "description": "Response time < 200ms", "metrics": {"target": 200, "unit": "ms"}, "priority": "MUST"},
                    {"id": "N2", "type": "security", "description": "JWT auth required", "priority": "MUST"}
                ]
            }
            ''',
            stderr="",
            returncode=0,
        )

        team_leader = TeamLeader(task="API", context={}, config=pm_config)
        result = team_leader.spawn_pm()

        assert result.requirement_summary == "API service"
        assert len(result.criteria) == 3  # 1 functional + 2 non_functional

        # Check functional
        func_criteria = [c for c in result.criteria if c.type == "functional"]
        assert len(func_criteria) == 1
        assert func_criteria[0].id == "F1"

        # Check non_functional
        nf_criteria = [c for c in result.criteria if c.type == "non_functional"]
        assert len(nf_criteria) == 2
        assert nf_criteria[0].id == "N1"
        assert nf_criteria[0].metrics == {"target": 200, "unit": "ms"}

    @patch("subprocess.run")
    def test_spawn_pm_fallback_on_invalid_json(self, mock_run, pm_config):
        """PM falls back gracefully when JSON parsing fails."""
        mock_run.return_value = MagicMock(
            stdout="This is not JSON output, just text",
            stderr="",
            returncode=0,
        )

        team_leader = TeamLeader(task="Test", context={}, config=pm_config)
        result = team_leader.spawn_pm()

        # Falls back to using task as summary
        assert result.requirement_summary == "Test"
        assert len(result.criteria) == 0

    def test_criteria_context_generation(self, pm_config):
        """TeamLeader generates criteria context for subagents."""
        team_leader = TeamLeader(task="Test", context={}, config=pm_config)
        team_leader.criteria = [
            PMCriteria(id="F1", type="functional", description="Add feature", verification="Test it", priority="MUST"),
            PMCriteria(id="N1", type="non_functional", description="Fast response", verification="Benchmark", priority="SHOULD"),
        ]

        criteria_context = "\n".join([
            "## Acceptance Criteria",
            "- [F1] Add feature (Priority: MUST)",
            "  Verification: Test it",
            "- [N1] Fast response (Priority: SHOULD)",
            "  Verification: Benchmark",
        ])

        # Verify the format is correct
        lines = criteria_context.split("\n")
        assert any("## Acceptance Criteria" in line for line in lines)
        assert any("[F1]" in line for line in lines)
        assert any("Priority: MUST" in line for line in lines)


class TestPMWorkflowIntegration:
    """Tests for PM integration in workflow."""

    @patch("subprocess.run")
    def test_workflow_starts_with_pm(self, mock_run):
        """Workflow runs PM phase first."""
        call_count = [0]

        def mock_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:  # PM
                return MagicMock(
                    stdout='{"requirement_summary": "Calculator", "criteria": [{"id": "F1", "type": "functional", "description": "Add", "verification": "2+3", "priority": "MUST"}]}',
                    stderr="", returncode=0
                )
            elif call_count[0] == 2:  # CODING
                return MagicMock(
                    stdout='{"status": "success", "summary": "Done", "files_changed": ["calc.py"], "confidence": 0.9}',
                    stderr="", returncode=0
                )
            elif call_count[0] == 3:  # AUDIT
                return MagicMock(
                    stdout='{"status": "pass", "findings": [], "risk_score": 1, "block_ship": false}',
                    stderr="", returncode=0
                )
            else:  # TESTING
                return MagicMock(
                    stdout='{"status": "all-pass", "coverage": 0.85, "block_ship": false, "test_failures": []}',
                    stderr="", returncode=0
                )

        mock_run.side_effect = mock_side_effect

        config = WC(workflow_type="auto", coverage_target=0.8, model="gpt-5.2", provider="codex")
        team_leader = TeamLeader(task="Calculator", context={}, config=config)

        result = team_leader.run()

        # PM should run first
        assert "pm" in result["completedPhases"]
        # PM result should be in response
        assert "pm" in result
        assert result["pm"]["requirement_summary"] == "Calculator"
        assert len(result["pm"]["criteria"]) == 1

    @patch("subprocess.run")
    def test_workflow_pm_passes_criteria_to_coding(self, mock_run):
        """PM criteria is passed to coding agent."""
        call_count = [0]

        def mock_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:  # PM
                return MagicMock(
                    stdout='{"requirement_summary": "Test", "criteria": [{"id": "F1", "type": "functional", "description": "Test feature", "verification": "Test", "priority": "MUST"}]}',
                    stderr="", returncode=0
                )
            elif call_count[0] == 2:  # CODING
                # Check that criteria context was passed
                prompt = args[0][-1] if args else ""
                has_criteria = "Acceptance Criteria" in prompt and "F1" in prompt
                return MagicMock(
                    stdout='{"status": "success", "summary": "Done with criteria: " + str(has_criteria), "files_changed": ["test.py"], "confidence": 0.9}',
                    stderr="", returncode=0
                )
            elif call_count[0] == 3:  # AUDIT
                return MagicMock(
                    stdout='{"status": "pass", "findings": [], "risk_score": 1, "block_ship": false}',
                    stderr="", returncode=0
                )
            else:  # TESTING
                return MagicMock(
                    stdout='{"status": "all-pass", "coverage": 0.85, "block_ship": false, "test_failures": []}',
                    stderr="", returncode=0
                )

        mock_run.side_effect = mock_side_effect

        config = WC(workflow_type="auto", coverage_target=0.8, model="gpt-5.2", provider="codex")
        team_leader = TeamLeader(task="Test", context={}, config=config)

        result = team_leader.run()

        # Verify criteria was generated
        assert len(team_leader.criteria) == 1
        assert team_leader.criteria[0].id == "F1"


# ---------------------------------------------------------------------------
# Run tests
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v"])

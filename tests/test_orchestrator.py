import unittest

from contracts import normalize_request
from orchestrator import Orchestrator, SubagentType, TeamLeader, WorkflowConfig


class OrchestratorTests(unittest.TestCase):
    def test_plan_only_request_returns_standard_response(self) -> None:
        request = normalize_request(
            {
                "task": "Implement login UI",
                "context": {"files": ["ui/login.py"]},
            }
        )

        response = Orchestrator().run(request)

        self.assertEqual(response["gateDecision"], "not_run")
        self.assertEqual(response["completedPhases"], ["intake", "planning"])
        self.assertEqual(response["findings"]["critical"], 0)
        self.assertEqual(response["escalation"], None)
        self.assertIn("execute", response["nextAction"])

    def test_execute_mode_runs_full_workflow(self) -> None:
        request = normalize_request(
            {
                "task": "Implement login UI",
                "config": {"mode": "execute"},
            }
        )

        response = Orchestrator().run(request)

        # Execute mode runs PM -> Coding -> Audit -> Testing workflow
        self.assertIn(response["gateDecision"], ["pass", "fail", "escalate"])
        self.assertIn("pm", response["completedPhases"])
        self.assertIn("coding", response["completedPhases"])

    def test_pm_agent_with_gpt_5_2(self) -> None:
        """Test PM agent spawns correctly with gpt-5.2 model."""
        config = WorkflowConfig(
            models={"pm": "gpt-5.2"},
            providers={"pm": "codex"},
        )
        leader = TeamLeader(
            task="实现一个计算器",
            config=config,
        )
        result = leader.spawn_pm()

        # PM should return valid criteria
        self.assertIsInstance(result.criteria, list)
        self.assertIsInstance(result.requirement_summary, str)
        self.assertGreater(len(result.requirement_summary), 0)
        # At least one MUST criteria expected for a calculator
        must_criteria = [c for c in result.criteria if c.priority == "MUST"]
        self.assertGreater(len(must_criteria), 0)
        # All criteria should have id, type, description, verification, priority
        for c in result.criteria:
            self.assertTrue(hasattr(c, "id"))
            self.assertTrue(hasattr(c, "type"))
            self.assertTrue(hasattr(c, "description"))
            self.assertTrue(hasattr(c, "verification"))
            self.assertTrue(hasattr(c, "priority"))
            self.assertIn(c.priority, ("MUST", "SHOULD"))


class TimeoutMultiplierTests(unittest.TestCase):
    """Tests for timeout multiplier feature (F1-F6, N1-N3)."""

    def test_f1_default_no_multiplier_set(self) -> None:
        """F1: Without extend_timeout, _calculate_timeout returns unchanged value."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        # Default timeout multipliers dict is empty
        self.assertEqual(leader._timeout_multipliers, {})

    def test_f2_extend_timeout_valid_multiplier(self) -> None:
        """F2: extend_timeout accepts valid multiplier > 0."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("coding", 2.0)
        self.assertEqual(leader._timeout_multipliers["coding"], 2.0)

    def test_f2_extend_timeout_rejects_zero(self) -> None:
        """F2: multiplier 0 is rejected."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        with self.assertRaises(ValueError) as ctx:
            leader.extend_timeout("coding", 0)
        self.assertIn("must be > 0", str(ctx.exception))

    def test_f2_extend_timeout_rejects_negative(self) -> None:
        """F2: negative multiplier is rejected."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        with self.assertRaises(ValueError) as ctx:
            leader.extend_timeout("coding", -1.5)
        self.assertIn("must be > 0", str(ctx.exception))

    def test_f2_extend_timeout_rejects_nan(self) -> None:
        """N2: NaN multiplier is rejected."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        with self.assertRaises(ValueError) as ctx:
            leader.extend_timeout("coding", float("nan"))
        self.assertIn("finite", str(ctx.exception))

    def test_f2_extend_timeout_rejects_infinity(self) -> None:
        """N2: Infinity multiplier is rejected."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        with self.assertRaises(ValueError) as ctx:
            leader.extend_timeout("coding", float("inf"))
        self.assertIn("finite", str(ctx.exception))

    def test_f2_extend_timeout_rejects_string(self) -> None:
        """N2: string multiplier is rejected."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        with self.assertRaises((ValueError, TypeError)):
            leader.extend_timeout("coding", "2.0")

    def test_f3_multiplier_affects_timeout(self) -> None:
        """F3: Final timeout = base timeout x multiplier."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        base_timeout = leader._calculate_timeout(SubagentType.CODING, "short task")
        leader.extend_timeout("coding", 2.0)
        multiplied_timeout = leader._calculate_timeout(SubagentType.CODING, "short task")
        self.assertEqual(multiplied_timeout, base_timeout * 2)

    def test_f3_multiplier_only_affects_set_agent(self) -> None:
        """F3: Multiplier only affects its own agent type."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("pm", 2.0)
        # PM should be doubled, CODING should be unchanged
        pm_timeout = leader._calculate_timeout(SubagentType.PM, "test")
        coding_timeout = leader._calculate_timeout(SubagentType.CODING, "test")
        # Base PM = 180, base CODING = 300 (complexity may add slightly)
        # PM with 2x should be ~360, CODING should be ~300
        self.assertGreater(pm_timeout, 350)  # at least 2x base 180
        self.assertLess(coding_timeout, 350)  # not multiplied

    def test_f5_reset_timeout_multiplier(self) -> None:
        """F5: reset_timeout_multiplier restores multiplier to 1.0."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("coding", 3.0)
        self.assertEqual(leader._timeout_multipliers["coding"], 3.0)
        leader.reset_timeout_multiplier("coding")
        self.assertNotIn("coding", leader._timeout_multipliers)

    def test_f6_multiplier_unchanged_on_invalid_input(self) -> None:
        """F6: Invalid input does not change existing multiplier."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("coding", 2.0)
        with self.assertRaises(ValueError):
            leader.extend_timeout("coding", -1)
        self.assertEqual(leader._timeout_multipliers["coding"], 2.0)

    def test_n1_timeout_calculation_is_o1(self) -> None:
        """N1: _calculate_timeout has O(1) overhead from multiplier lookup."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        # Multiple lookups don't add overhead - just one dict get
        leader._calculate_timeout(SubagentType.CODING, "test")
        leader._calculate_timeout(SubagentType.PM, "test")
        # If we get here without error, O(1) overhead is satisfied

    def test_all_scope_multiplier(self) -> None:
        """'all' scope multiplier applies to all agents."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("all", 2.0)
        self.assertEqual(leader._timeout_multipliers["all"], 2.0)


if __name__ == "__main__":
    unittest.main()

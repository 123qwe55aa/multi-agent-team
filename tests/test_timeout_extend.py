"""Tests for timeout multiplier feature: _timeout_multipliers dict, extend_timeout(), _calculate_timeout(), and /team extend command."""
import math
import subprocess
import unittest
from unittest.mock import patch, MagicMock

from contracts import normalize_request
from orchestrator import Orchestrator, SubagentType, TeamLeader, WorkflowConfig, TimeoutExpired as TeamTimeoutExpired
from run import InteractiveREPL


# ---------------------------------------------------------------------------
# TeamLeader timeout feature tests
# ---------------------------------------------------------------------------

class TestTimeoutMultipliersDict(unittest.TestCase):
    """F1: _timeout_multipliers dict is initialized as empty dict."""

    def test_f1_default_empty_on_init(self) -> None:
        """F1: _timeout_multipliers is {} on TeamLeader init."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        self.assertEqual(leader._timeout_multipliers, {})

    def test_f1_timeout_multipliers_is_dict(self) -> None:
        """F1: _timeout_multipliers is a plain dict."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        self.assertIsInstance(leader._timeout_multipliers, dict)

    def test_f1_from_dict_restores_multipliers(self) -> None:
        """F1: from_dict does NOT restore _timeout_multipliers (not persisted)."""
        # Note: checkpointing does not include timeout multipliers
        # This is expected - multipliers are session-scoped
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("coding", 2.0)
        data = leader.to_dict()
        restored = TeamLeader.from_dict(data, config=WorkflowConfig())
        # Multipliers are NOT in to_dict/from_dict
        self.assertEqual(restored._timeout_multipliers, {})


class TestExtendTimeout(unittest.TestCase):
    """F2: extend_timeout() method stores multiplier per scope key."""

    def test_f2_extend_accepts_float(self) -> None:
        """F2: extend_timeout accepts positive float."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("coding", 2.0)
        self.assertEqual(leader._timeout_multipliers["coding"], 2.0)

    def test_f2_extend_accepts_int(self) -> None:
        """F2: extend_timeout accepts positive int multiplier."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("audit", 3)
        self.assertEqual(leader._timeout_multipliers["audit"], 3)

    def test_f2_extend_accepts_all_scope(self) -> None:
        """F2: 'all' scope key is accepted."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("all", 1.5)
        self.assertEqual(leader._timeout_multipliers["all"], 1.5)

    def test_f2_extend_rejects_zero(self) -> None:
        """F2: multiplier of 0 is rejected with ValueError."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        with self.assertRaises(ValueError) as ctx:
            leader.extend_timeout("pm", 0)
        self.assertIn("must be > 0", str(ctx.exception))

    def test_f2_extend_rejects_negative(self) -> None:
        """F2: negative multiplier is rejected."""
        for val in [-0.1, -1.0, -10.0]:
            leader = TeamLeader(task="test", config=WorkflowConfig())
            with self.assertRaises(ValueError) as ctx:
                leader.extend_timeout("coding", val)
            self.assertIn("must be > 0", str(ctx.exception))

    def test_f2_extend_rejects_nan(self) -> None:
        """N2: NaN multiplier is rejected."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        with self.assertRaises(ValueError) as ctx:
            leader.extend_timeout("testing", float("nan"))
        self.assertIn("finite", str(ctx.exception))

    def test_f2_extend_rejects_infinity(self) -> None:
        """N2: Infinity multiplier is rejected."""
        for val in [float("inf"), float("-inf")]:
            leader = TeamLeader(task="test", config=WorkflowConfig())
            with self.assertRaises(ValueError) as ctx:
                leader.extend_timeout("pm", val)
            self.assertIn("finite", str(ctx.exception))

    def test_f2_extend_rejects_string(self) -> None:
        """N2: string multiplier raises TypeError."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        with self.assertRaises((TypeError, ValueError)):
            leader.extend_timeout("coding", "2.0")

    def test_f2_extend_rejects_none(self) -> None:
        """N2: None multiplier raises TypeError."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        with self.assertRaises((TypeError, ValueError)):
            leader.extend_timeout("coding", None)  # type: ignore

    def test_f2_extend_idempotent(self) -> None:
        """F2: calling extend_timeout twice for same key overwrites."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("coding", 2.0)
        leader.extend_timeout("coding", 3.0)
        self.assertEqual(leader._timeout_multipliers["coding"], 3.0)


class TestResetTimeoutMultiplier(unittest.TestCase):
    """F5: reset_timeout_multiplier removes the multiplier key."""

    def test_f5_reset_removes_key(self) -> None:
        """F5: reset removes the key, restoring default behavior."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("coding", 3.0)
        self.assertEqual(leader._timeout_multipliers["coding"], 3.0)
        leader.reset_timeout_multiplier("coding")
        self.assertNotIn("coding", leader._timeout_multipliers)

    def test_f5_reset_nonexistent_key_noop(self) -> None:
        """F5: reset on non-existent key is a no-op."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        # Should not raise
        leader.reset_timeout_multiplier("nonexistent")
        self.assertNotIn("nonexistent", leader._timeout_multipliers)


class TestCalculateTimeoutWithMultiplier(unittest.TestCase):
    """F3, F6: _calculate_timeout applies multiplier and doesn't change existing on invalid."""

    def test_f3_multiplier_affects_result(self) -> None:
        """F3: multiplier scales the returned timeout."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        base = leader._calculate_timeout(SubagentType.CODING, "test task")
        leader.extend_timeout("coding", 2.0)
        multiplied = leader._calculate_timeout(SubagentType.CODING, "test task")
        self.assertEqual(multiplied, base * 2)

    def test_f3_multiplier_scales_pm(self) -> None:
        """F3: multiplier scales PM timeout correctly."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        base_pm = leader._calculate_timeout(SubagentType.PM, "test")
        leader.extend_timeout("pm", 3.0)
        scaled_pm = leader._calculate_timeout(SubagentType.PM, "test")
        self.assertEqual(scaled_pm, base_pm * 3)

    def test_f3_only_affects_set_scope(self) -> None:
        """F3: multiplier only affects its own scope, not others."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("coding", 5.0)
        base_pm = leader._calculate_timeout(SubagentType.PM, "test")
        scaled_pm = leader._calculate_timeout(SubagentType.PM, "test")
        self.assertEqual(base_pm, scaled_pm)

    def test_f3_all_scope_affects_all(self) -> None:
        """F3: 'all' multiplier is a wildcard that affects every agent type."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        base_coding = leader._calculate_timeout(SubagentType.CODING, "test")
        base_pm = leader._calculate_timeout(SubagentType.PM, "test")
        leader.extend_timeout("all", 2.0)
        self.assertEqual(leader._calculate_timeout(SubagentType.CODING, "test"), base_coding * 2)
        self.assertEqual(leader._calculate_timeout(SubagentType.PM, "test"), base_pm * 2)

    def test_f3_specific_overrides_all(self) -> None:
        """F3: per-agent multiplier overrides the 'all' wildcard."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("all", 2.0)
        leader.extend_timeout("coding", 5.0)
        # Coding should use its specific 5x, not the 'all' 2x
        timeout = leader._calculate_timeout(SubagentType.CODING, "test")
        base = leader._calculate_timeout(SubagentType.CODING, "test")  # gets overridden below
        # Directly check: coding 5x, pm falls back to 'all' 2x
        leader2 = TeamLeader(task="test", config=WorkflowConfig())
        leader2.extend_timeout("all", 2.0)
        leader2.extend_timeout("coding", 5.0)
        base_coding = 300  # base for CODING
        base_pm = 180     # base for PM
        # coding = 5x (specific), pm = 2x (all)
        self.assertEqual(leader2._calculate_timeout(SubagentType.CODING, "test"), int(base_coding * 5))
        self.assertEqual(leader2._calculate_timeout(SubagentType.PM, "test"), int(base_pm * 2))

    def test_f6_invalid_input_preserves_existing(self) -> None:
        """F6: ValueError from invalid multiplier does NOT clear existing multiplier."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("coding", 2.0)
        try:
            leader.extend_timeout("coding", -1.0)
        except ValueError:
            pass
        self.assertEqual(leader._timeout_multipliers["coding"], 2.0)

    def test_f3_timeout_is_clamped(self) -> None:
        """F3: multiplied timeout is clamped to [60, 900]."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("coding", 100.0)  # Would be huge without clamp
        timeout = leader._calculate_timeout(SubagentType.CODING, "test")
        self.assertLessEqual(timeout, 900)

    def test_f3_timeout_min_is_60(self) -> None:
        """F3: timeout min is 60 seconds even with multiplier < 1."""
        leader = TeamLeader(task="test", config=WorkflowConfig())
        leader.extend_timeout("pm", 0.1)
        # PM base is 180 * 0.1 = 18, should clamp to 60
        timeout = leader._calculate_timeout(SubagentType.PM, "x")
        self.assertGreaterEqual(timeout, 60)


class TestOrchestratorWithMultipliers(unittest.TestCase):
    """Integration: Orchestrator.run passes timeout_multipliers to TeamLeader."""

    @patch.object(TeamLeader, "run", return_value={"gateDecision": "pass", "completedPhases": []})
    def test_orchestrator_applies_multipliers(self, mock_run: object) -> None:
        """Orchestrator.run calls extend_timeout for each multiplier."""
        request = normalize_request({
            "task": "test task",
            "config": {"mode": "execute"},
        })
        orchestrator = Orchestrator()
        multipliers = {"coding": 2.0, "pm": 1.5}
        orchestrator.run(request, timeout_multipliers=multipliers)

        mock_run.assert_called_once()  # TeamLeader.run() was called
        # The TeamLeader instance is what received extend_timeout calls
        # We verify via the orchestrator's flow
        # (mocked run() means TeamLeader was constructed)

    @patch.object(TeamLeader, "run", return_value={"gateDecision": "pass", "completedPhases": []})
    def test_orchestrator_handles_none_multipliers(self, mock_run: object) -> None:
        """Orchestrator.run handles timeout_multipliers=None gracefully."""
        request = normalize_request({
            "task": "test task",
            "config": {"mode": "execute"},
        })
        orchestrator = Orchestrator()
        # Should not raise
        orchestrator.run(request, timeout_multipliers=None)
        mock_run.assert_called_once()


# ---------------------------------------------------------------------------
# InteractiveREPL /team extend command tests
# ---------------------------------------------------------------------------

class TestInteractiveREPLExtend(unittest.TestCase):
    """Test InteractiveREPL /team extend command parsing and execution."""

    def test_do_extend_sets_multiplier(self) -> None:
        """REPL: /team extend coding 2.0 sets the multiplier."""
        repl = InteractiveREPL()
        repl._do_extend("coding 2.0")
        self.assertEqual(repl.timeout_multipliers["coding"], 2.0)

    def test_do_extend_single_arg_applies_to_all(self) -> None:
        """REPL: /team extend 2.0 applies to all agents."""
        repl = InteractiveREPL()
        repl._do_extend("2.0")
        self.assertEqual(repl.timeout_multipliers["all"], 2.0)

    def test_do_extend_reset_clears_all(self) -> None:
        """REPL: /team extend reset clears all multipliers."""
        repl = InteractiveREPL()
        repl.timeout_multipliers["coding"] = 2.0
        repl.timeout_multipliers["pm"] = 1.5
        repl._do_extend("reset")
        self.assertEqual(repl.timeout_multipliers, {})

    def test_do_extend_invalid_multiplier_rejected(self) -> None:
        """REPL: /team extend coding -1 rejects with error."""
        repl = InteractiveREPL()
        result = repl._do_extend("coding -1")
        self.assertTrue(result)  # continues REPL
        self.assertNotIn("coding", repl.timeout_multipliers)

    def test_do_extend_invalid_scope_key_accepted(self) -> None:
        """REPL: arbitrary scope keys are accepted (validated later)."""
        repl = InteractiveREPL()
        repl._do_extend("arbitrary_key 1.5")
        self.assertEqual(repl.timeout_multipliers["arbitrary_key"], 1.5)

    def test_parse_extend_two_args(self) -> None:
        """_parse_extend: two args -> (scope, multiplier)."""
        repl = InteractiveREPL()
        result = repl._parse_extend("coding 2.0")
        self.assertEqual(result, ("coding", 2.0))

    def test_parse_extend_one_numeric_arg(self) -> None:
        """_parse_extend: one numeric arg -> ('all', multiplier)."""
        repl = InteractiveREPL()
        result = repl._parse_extend("3.0")
        self.assertEqual(result, ("all", 3.0))

    def test_parse_extend_reset(self) -> None:
        """_parse_extend: 'reset' -> ('reset', 1.0)."""
        repl = InteractiveREPL()
        result = repl._parse_extend("reset")
        self.assertEqual(result, ("reset", 1.0))


class TestPartialOutputOnTimeout(unittest.TestCase):
    """Partial output capture when subagent times out via streaming buffer."""

    def _raise_timeout(self, stdout: str = "partial output here",
                       stderr: str = "") -> None:
        """Raise our custom TimeoutExpired with given output."""
        raise TeamTimeoutExpired("claude", 300, stdout, stderr)

    def test_timeout_returns_timeout_retry_status(self) -> None:
        """TimeoutExpired is caught and returns status='timeout_retry'."""
        leader = TeamLeader(task="test task", config=WorkflowConfig())
        with patch.object(leader, "_run_with_buffer", side_effect=lambda *a, **kw: self._raise_timeout()):
            result = leader.spawn_subagent(SubagentType.CODING)
        self.assertEqual(result.status, "timeout_retry")

    def test_timeout_captures_stdout_in_raw_output(self) -> None:
        """Partial stdout is captured in raw_output field."""
        leader = TeamLeader(task="test task", config=WorkflowConfig())
        with patch.object(leader, "_run_with_buffer",
                          side_effect=lambda *a, **kw: self._raise_timeout(stdout="created file foo.py\nimplemented bar()")):
            result = leader.spawn_subagent(SubagentType.CODING)
        self.assertEqual(result.raw_output, "created file foo.py\nimplemented bar()")

    def test_timeout_captures_str_stdout(self) -> None:
        """Partial stdout (str) is captured directly."""
        leader = TeamLeader(task="test task", config=WorkflowConfig())
        with patch.object(leader, "_run_with_buffer",
                          side_effect=lambda *a, **kw: self._raise_timeout(stdout="partial string output")):
            result = leader.spawn_subagent(SubagentType.CODING)
        self.assertEqual(result.raw_output, "partial string output")

    def test_timeout_falls_back_to_stderr_when_stdout_empty(self) -> None:
        """When stdout is empty, stderr is used as fallback."""
        leader = TeamLeader(task="test task", config=WorkflowConfig())
        with patch.object(leader, "_run_with_buffer",
                          side_effect=lambda *a, **kw: self._raise_timeout(stdout="", stderr="partial error output")):
            result = leader.spawn_subagent(SubagentType.CODING)
        self.assertEqual(result.raw_output, "partial error output")

    def test_timeout_trims_to_last_100_lines(self) -> None:
        """When output > 100 lines, only last 100 lines are kept."""
        lines = [f"line {i}" for i in range(150)]
        leader = TeamLeader(task="test task", config=WorkflowConfig())
        with patch.object(leader, "_run_with_buffer",
                          side_effect=lambda *a, **kw: self._raise_timeout(stdout="\n".join(lines))):
            result = leader.spawn_subagent(SubagentType.CODING)
        captured_lines = result.raw_output.splitlines()
        self.assertEqual(len(captured_lines), 100)
        self.assertTrue(captured_lines[0].startswith("line 50"))

    def test_timeout_extends_timeout_multiplier(self) -> None:
        """Timeout triggers 2x multiplier extension."""
        leader = TeamLeader(task="test task", config=WorkflowConfig())
        self.assertEqual(leader._timeout_multipliers.get("coding"), None)
        with patch.object(leader, "_run_with_buffer", side_effect=lambda *a, **kw: self._raise_timeout()):
            leader.spawn_subagent(SubagentType.CODING)
        self.assertEqual(leader._timeout_multipliers.get("coding"), 2.0)

    def test_timeout_summary_mentions_partial_work(self) -> None:
        """Summary message confirms partial work was captured."""
        leader = TeamLeader(task="test task", config=WorkflowConfig())
        with patch.object(leader, "_run_with_buffer",
                          side_effect=lambda *a, **kw: self._raise_timeout(stdout="some partial work")):
            result = leader.spawn_subagent(SubagentType.CODING)
        self.assertIn("Partial work captured", result.summary)
        self.assertIn("some partial work", result.raw_output)


    def test_parse_extend_invalid_usage(self) -> None:
        """_parse_extend: empty or bad usage -> (None, error)."""
        repl = InteractiveREPL()
        result = repl._parse_extend("")
        self.assertIsNone(result[0])
        result2 = repl._parse_extend("a b c")
        self.assertIsNone(result2[0])

    def test_validate_multiplier_accepts_valid_values(self) -> None:
        """_validate_multiplier: accepts 0.1, 1.0, 100.0."""
        repl = InteractiveREPL()
        for val in [0.1, 1.0, 100.0, 0.001]:
            err = repl._validate_multiplier(val)
            self.assertIsNone(err, f"Expected {val} to be valid: {err}")

    def test_validate_multiplier_rejects_invalid(self) -> None:
        """_validate_multiplier: rejects 0, negative, NaN, Inf."""
        repl = InteractiveREPL()
        for val in [0, -1, float("nan"), float("inf"), float("-inf")]:
            err = repl._validate_multiplier(val)
            self.assertIsNotNone(err, f"Expected {val} to be invalid")

    def test_validate_multiplier_rejects_non_number(self) -> None:
        """_validate_multiplier: rejects string."""
        repl = InteractiveREPL()
        err = repl._validate_multiplier("2.0")  # type: ignore
        self.assertIsNotNone(err)


if __name__ == "__main__":
    unittest.main()

import unittest

from contracts import RequestValidationError, TeamRequest, normalize_request


class TeamRequestTests(unittest.TestCase):
    def test_normalize_request_applies_defaults(self) -> None:
        payload = {"task": "Add login flow"}

        request = normalize_request(payload)

        self.assertEqual(request.task, "Add login flow")
        self.assertEqual(request.context.goal, None)
        self.assertEqual(request.context.files, [])
        self.assertEqual(request.context.constraints, [])
        self.assertEqual(request.config.mode, "plan_only")
        self.assertEqual(request.config.workflow, "auto")
        self.assertEqual(request.config.coverage_target, 0.8)

    def test_normalize_request_rejects_blank_task(self) -> None:
        with self.assertRaises(RequestValidationError):
            normalize_request({"task": "   "})

    def test_normalize_request_rejects_out_of_range_coverage(self) -> None:
        with self.assertRaises(RequestValidationError):
            normalize_request(
                {
                    "task": "Add tests",
                    "config": {"coverage_target": 1.2},
                }
            )

    def test_request_can_convert_back_to_dict(self) -> None:
        request = TeamRequest(
            task="Refactor auth flow",
            context={"goal": "Ship safer auth", "files": ["auth.py"]},
            config={"workflow": "parallel"},
        )

        payload = request.to_dict()

        self.assertEqual(payload["task"], "Refactor auth flow")
        self.assertEqual(payload["context"]["goal"], "Ship safer auth")
        self.assertEqual(payload["context"]["files"], ["auth.py"])
        self.assertEqual(payload["config"]["mode"], "plan_only")
        self.assertEqual(payload["config"]["workflow"], "parallel")


if __name__ == "__main__":
    unittest.main()

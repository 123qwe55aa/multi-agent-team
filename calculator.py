"""Simple calculator with JSON output."""

from dataclasses import dataclass, asdict
from typing import Literal


Operation = Literal["add", "subtract", "multiply", "divide", "power", "sqrt"]


@dataclass
class CalculationResult:
    status: Literal["success", "error"]
    operation: Operation
    operands: list[float]
    result: float | None
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def calculate(
    operation: Operation,
    *operands: float,
) -> CalculationResult:
    """Perform a calculation and return JSON-serializable result."""

    try:
        match operation:
            case "add":
                result = sum(operands)
            case "subtract":
                if len(operands) != 2:
                    raise ValueError("Subtract requires exactly 2 operands")
                result = operands[0] - operands[1]
            case "multiply":
                result = 1
                for op in operands:
                    result *= op
            case "divide":
                if len(operands) != 2:
                    raise ValueError("Divide requires exactly 2 operands")
                if operands[1] == 0:
                    raise ZeroDivisionError("Cannot divide by zero")
                result = operands[0] / operands[1]
            case "power":
                if len(operands) != 2:
                    raise ValueError("Power requires exactly 2 operands")
                result = operands[0] ** operands[1]
            case "sqrt":
                if len(operands) != 1:
                    raise ValueError("Sqrt requires exactly 1 operand")
                if operands[0] < 0:
                    raise ValueError("Cannot take sqrt of negative number")
                result = operands[0] ** 0.5
            case _:
                raise ValueError(f"Unknown operation: {operation}")

        return CalculationResult(
            status="success",
            operation=operation,
            operands=list(operands),
            result=result,
        )

    except Exception as e:
        return CalculationResult(
            status="error",
            operation=operation,
            operands=list(operands),
            result=None,
            error=str(e),
        )


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: python calculator.py <operation> <operands...>"}))
        sys.exit(1)

    op = sys.argv[1]
    try:
        nums = [float(x) for x in sys.argv[2:]]
    except ValueError:
        print(json.dumps({"error": "All operands must be numbers"}))
        sys.exit(1)

    result = calculate(op, *nums)
    print(json.dumps(result.to_dict(), indent=2))
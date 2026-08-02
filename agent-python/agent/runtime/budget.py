from __future__ import annotations

from dataclasses import dataclass

from agent.v1 import agent_execution_pb2 as proto


class BudgetExhausted(RuntimeError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class BudgetVector:
    model_attempts: int = 0
    tool_calls: int = 0
    iterations: int = 0
    replans: int = 0
    supplements: int = 0
    quality_repairs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    elapsed_ms: int = 0

    @classmethod
    def from_proto(cls, value: proto.BudgetDelta | proto.ConsumedBudget | None) -> BudgetVector:
        if value is None:
            return cls()
        return cls(**{name: int(getattr(value, name)) for name in cls.__dataclass_fields__})

    def add(self, other: BudgetVector) -> BudgetVector:
        return BudgetVector(**{name: getattr(self, name) + getattr(other, name) for name in self.__dataclass_fields__})

    def subtract_floor(self, other: BudgetVector) -> BudgetVector:
        return BudgetVector(**{name: max(0, getattr(self, name) - getattr(other, name)) for name in self.__dataclass_fields__})

    def as_proto(self) -> proto.BudgetDelta:
        return proto.BudgetDelta(**{name: getattr(self, name) for name in self.__dataclass_fields__})


_LIMITS = {
    "model_attempts": ("max_model_attempts", "MODEL_ATTEMPT_BUDGET_EXHAUSTED"),
    "tool_calls": ("max_tool_calls", "TOOL_CALL_BUDGET_EXHAUSTED"),
    "iterations": ("max_iterations", "ITERATION_BUDGET_EXHAUSTED"),
    "replans": ("max_replans", "REPLAN_BUDGET_EXHAUSTED"),
    "supplements": ("max_supplements", "SUPPLEMENT_BUDGET_EXHAUSTED"),
    "quality_repairs": ("max_quality_repairs", "QUALITY_REPAIR_BUDGET_EXHAUSTED"),
    "input_tokens": ("max_input_tokens", "INPUT_TOKEN_BUDGET_EXHAUSTED"),
    "output_tokens": ("max_output_tokens", "OUTPUT_TOKEN_BUDGET_EXHAUSTED"),
    "elapsed_ms": ("max_elapsed_ms", "ELAPSED_TIME_BUDGET_EXHAUSTED"),
}


@dataclass
class BudgetState:
    limits: proto.RunBudget
    consumed: BudgetVector
    reserved: BudgetVector = BudgetVector()

    def reserve(self, delta: BudgetVector) -> None:
        self._check(self.consumed.add(self.reserved), delta)
        self.reserved = self.reserved.add(delta)

    def finish(self, reservation: BudgetVector, consumption: BudgetVector) -> None:
        self.reserved = self.reserved.subtract_floor(reservation)
        self.consumed = self.consumed.add(consumption)

    def consume(self, delta: BudgetVector) -> None:
        self._check(self.consumed, delta)
        self.consumed = self.consumed.add(delta)

    def remaining(self) -> BudgetVector:
        used = self.consumed.add(self.reserved)
        return BudgetVector(**{name: max(0, int(getattr(self.limits, limit)) - getattr(used, name)) for name, (limit, _) in _LIMITS.items()})

    def _check(self, used: BudgetVector, requested: BudgetVector) -> None:
        for name, (limit_name, reason) in _LIMITS.items():
            value = getattr(requested, name)
            if value < 0:
                raise ValueError("budget deltas must be non-negative")
            if getattr(used, name) + value > int(getattr(self.limits, limit_name)):
                raise BudgetExhausted(reason)

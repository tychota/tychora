"""Bounded, explicit evidence requirements and request-context conditions."""

from typing import Literal, Self

from pydantic import model_validator

from .base import Contract, FrozenJsonMap, NonEmptyStr, PositiveInt
from .requests import QueryRequest


class ResponseRequirement(Contract):
    requirement_id: NonEmptyStr
    description: str = ""
    target: Literal["lane", "kind", "record_schema"]
    value: NonEmptyStr
    minimum: PositiveInt = 1
    when: FrozenJsonMap | None = None

    @model_validator(mode="after")
    def bounded_condition(self) -> Self:
        if self.when is not None:
            _validate_when(self.when, 0)
        return self


def _validate_when(node: FrozenJsonMap, depth: int) -> None:
    if depth >= 8 or len(node) != 1:
        raise ValueError("Requirement condition is invalid or too deep")
    operator, operand = next(iter(node.items()))
    if operator in {"eq", "in"}:
        if (
            not isinstance(operand, tuple)
            or len(operand) != 2
            or not isinstance(operand[0], str)
            or not operand[0]
        ):
            raise ValueError("Requirement comparison is invalid")
        if operator == "in" and (
            not isinstance(operand[1], tuple) or not 0 < len(operand[1]) <= 32
        ):
            raise ValueError("Requirement membership is invalid")
    elif operator == "exists":
        if not isinstance(operand, str) or not operand:
            raise ValueError("Requirement existence check is invalid")
    elif operator in {"and", "or"}:
        if (
            not isinstance(operand, tuple)
            or not 0 < len(operand) <= 16
            or any(not isinstance(child, FrozenJsonMap) for child in operand)
        ):
            raise ValueError("Requirement expression is invalid")
        for child in operand:
            if not isinstance(child, FrozenJsonMap):
                raise ValueError("Requirement expression is invalid")  # noqa: TRY004 - Pydantic validation
            _validate_when(child, depth + 1)
    else:
        raise ValueError("Unsupported requirement operator")


def _lookup(request: QueryRequest, key: str):
    if key == "mode":
        return request.mode
    value = request.context
    for part in key.split("."):
        if not isinstance(value, FrozenJsonMap) or part not in value:
            return _MISSING
        value = value[part]
    return value


_MISSING = object()


def _evaluate(node: FrozenJsonMap, request: QueryRequest) -> bool:
    op, operand = next(iter(node.items()))
    if op in {"eq", "in"}:
        if (
            not isinstance(operand, tuple)
            or len(operand) != 2
            or not isinstance(operand[0], str)
        ):
            raise ValueError("Requirement comparison is invalid")
        value = _lookup(request, operand[0])
        if value is _MISSING:
            return False
        if op == "eq":
            return value == operand[1]
        if not isinstance(operand[1], tuple):
            raise ValueError("Requirement membership is invalid")
        return value in operand[1]
    if op == "exists":
        if not isinstance(operand, str):
            raise ValueError("Requirement existence check is invalid")
        return _lookup(request, operand) is not _MISSING
    if not isinstance(operand, tuple):
        raise ValueError("Requirement expression is invalid")  # noqa: TRY004 - validated condition
    children = tuple(child for child in operand if isinstance(child, FrozenJsonMap))
    if len(children) != len(operand):
        raise ValueError("Requirement expression is invalid")
    if op == "and":
        return all(_evaluate(child, request) for child in children)
    return any(_evaluate(child, request) for child in children)


def active_requirements(
    requirements: tuple[ResponseRequirement, ...], request: QueryRequest
) -> tuple[ResponseRequirement, ...]:
    """Evaluate before search from mode and explicitly supplied context only."""
    return tuple(
        item
        for item in requirements
        if item.when is None or _evaluate(item.when, request)
    )

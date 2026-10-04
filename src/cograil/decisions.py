"""Decision tables (ADR 0009): load-time checks, deterministic evaluation, audit detail.

A table in decisions/<name>.yaml has typed inputs, rules and typed outputs. Under hit_policy
`first` the first rule, in table order, whose conditions all hold decides, and no match is a
DecisionError; under `collect` every matching rule is returned, in table order. A rule's
`when` maps an input to a condition, and an input it leaves out matches anything:

- a value: the input equals it;
- a list: the input equals one of its values;
- for an integer or number input, a string such as ">10", "<=5" or "=3": a comparison.

Every rule's `then` sets every output, each of its declared type. The model supplies the
inputs; the table decides. A `decision` Tool named `<anything>.<table>` evaluates the table
named by the part after its last dot, and the registry writes `DecisionOutcome.audit_detail`
(table name, version and the ids of the rules that fired) as a `decision.evaluated` AuditEvent.
"""

from __future__ import annotations

import itertools
import math
import operator
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from cograil.domain import Decision, DecisionField, DecisionRule, Tool
from cograil.errors import DecisionError

Condition = Callable[[Any], bool]

_COMPARISON = re.compile(r"^\s*(<=|>=|<|>|=)?\s*(-?\d+(?:\.\d+)?)\s*$")
_OPERATORS: dict[str, Callable[[Any, Any], bool]] = {
    "<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "=": operator.eq,
}  # fmt: skip
_IS_TYPE: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v),
}
_BOOLEANS = {"true": True, "false": False}
_PARSE: dict[str, Callable[[str], Any]] = {
    "string": str,
    "boolean": lambda raw: _BOOLEANS[raw.strip().lower()],
    "integer": int,
    "number": float,
}


@dataclass(frozen=True)
class DecisionOutcome:
    """What a table decided: the rules that fired, in table order, with their outputs."""

    table: str
    version: int
    hit_policy: str
    matches: tuple[tuple[str, dict[str, Any]], ...]

    def as_result(self) -> dict[str, Any]:
        """The Tool result: one rule and its outputs under `first`, every match under `collect`."""
        head: dict[str, Any] = {"table": self.table, "version": self.version}
        if self.hit_policy == "first":
            if not self.matches:
                raise DecisionError(f"{self.table} v{self.version}: no rule matched")
            rule, outputs = self.matches[0]
            return {**head, "rule": rule, "outputs": outputs}
        return {**head, "matches": [{"rule": r, "outputs": o} for r, o in self.matches]}

    def audit_detail(self) -> dict[str, Any]:
        """The `decision.evaluated` AuditEvent's detail; the inputs stay in the ToolCall."""
        rules = [rule for rule, _ in self.matches]
        return {"table": self.table, "version": self.version, "hit_policy": self.hit_policy,
                "rules": rules}  # fmt: skip


@dataclass(frozen=True)
class _Rule:
    id: str
    conditions: dict[str, Condition]
    outputs: dict[str, Any]

    def holds(self, values: Mapping[str, Any]) -> bool:
        return all(condition(values[name]) for name, condition in self.conditions.items())


class DecisionTable:
    """A checked Decision, ready to evaluate; DecisionError if the table is invalid."""

    def __init__(self, decision: Decision) -> None:
        ids = [rule.id for rule in decision.rules]
        if repeated := sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1}):
            raise DecisionError(f"{decision.name}: rule ids {repeated} are used more than once")
        self.decision = decision
        self._rules = [_compile(decision, rule) for rule in decision.rules]

    def evaluate(self, inputs: Mapping[str, Any]) -> DecisionOutcome:
        """Decide from inputs; a missing, unknown or mistyped input is a DecisionError."""
        decision = self.decision
        values = _check_inputs(decision, inputs)
        fired = (rule for rule in self._rules if rule.holds(values))
        chosen = list(itertools.islice(fired, 1) if decision.hit_policy == "first" else fired)
        if decision.hit_policy == "first" and not chosen:
            raise DecisionError(f"{decision.name} v{decision.version}: no rule matched")
        matches = tuple((rule.id, dict(rule.outputs)) for rule in chosen)
        return DecisionOutcome(decision.name, decision.version, decision.hit_policy, matches)

    async def __call__(self, args: dict[str, Any]) -> DecisionOutcome:
        """The `decision` Tool kind's Invoke."""
        return self.evaluate(args)


def table_for(decisions: list[Decision], name: str) -> DecisionTable:
    found = next((decision for decision in decisions if decision.name == name), None)
    if found is None:
        raise DecisionError(f"no decision table {name!r}")
    return DecisionTable(found)


def table_name(tool: Tool) -> str:
    """The table a `decision` Tool evaluates: `decide.approval_routing` -> approval_routing."""
    return tool.name.rsplit(".", 1)[-1]


def parse_inputs(decision: Decision, pairs: list[str]) -> dict[str, Any]:
    """`name=value` pairs from the command line, each read as its input's declared type."""
    inputs: dict[str, Any] = {}
    for pair in pairs:
        name, sep, raw = pair.partition("=")
        name = name.strip()
        if not sep or name not in decision.inputs:
            raise DecisionError(
                f"{pair!r}: expected name=value, name one of {sorted(decision.inputs)}"
            )
        if name in inputs:
            raise DecisionError(f"input {name} is given more than once")
        kind = decision.inputs[name].type
        try:
            inputs[name] = _PARSE[kind](raw)
        except (KeyError, ValueError) as exc:
            raise DecisionError(f"input {name}: {raw!r} is not of type {kind}") from exc
    return inputs


def _compile(decision: Decision, rule: DecisionRule) -> _Rule:
    where = f"{decision.name} rule {rule.id}"
    if unknown := sorted(rule.when.keys() - decision.inputs.keys()):
        raise DecisionError(f"{where}: when names unknown inputs {unknown}")
    if rule.then.keys() != decision.outputs.keys():
        raise DecisionError(
            f"{where}: then must set exactly the outputs {sorted(decision.outputs)}"
        )
    for name, value in rule.then.items():
        if not _IS_TYPE[decision.outputs[name].type](value):
            raise DecisionError(
                f"{where}: output {name} must be of type {decision.outputs[name].type}"
            )
    conditions = {
        name: _condition(f"{where}, input {name}", decision.inputs[name], spec)
        for name, spec in rule.when.items()
    }
    return _Rule(rule.id, conditions, dict(rule.then))


def _condition(where: str, field: DecisionField, spec: Any) -> Condition:
    is_type = _IS_TYPE[field.type]
    if isinstance(spec, list):
        if not spec or not all(is_type(value) for value in spec):
            raise DecisionError(f"{where}: a list needs one or more {field.type} values")
        allowed = list(spec)
        return lambda value: value in allowed
    if isinstance(spec, str) and field.type in ("integer", "number"):
        match = _COMPARISON.match(spec)
        if match is None:
            raise DecisionError(f"{where}: {spec!r} is not a comparison such as '>10'")
        compare, bound = _OPERATORS[match.group(1) or "="], float(match.group(2))
        return lambda value: compare(value, bound)
    if not is_type(spec):
        raise DecisionError(f"{where}: {spec!r} is not of type {field.type}")
    return lambda value: bool(value == spec)


def _check_inputs(decision: Decision, inputs: Mapping[str, Any]) -> dict[str, Any]:
    where = f"{decision.name} v{decision.version}"
    if missing := sorted(decision.inputs.keys() - inputs.keys()):
        raise DecisionError(f"{where}: missing inputs {missing}")
    if unknown := sorted(inputs.keys() - decision.inputs.keys()):
        raise DecisionError(f"{where}: unknown inputs {unknown}")
    values: dict[str, Any] = {}
    for name, field in decision.inputs.items():
        value = inputs[name]
        if field.type == "integer" and isinstance(value, float) and value.is_integer():
            value = int(value)  # JSON Schema's integer admits 3.0
        if not _IS_TYPE[field.type](value):
            raise DecisionError(
                f"{where}: input {name} must be of type {field.type}, not {type(value).__name__}"
            )
        values[name] = value
    return values

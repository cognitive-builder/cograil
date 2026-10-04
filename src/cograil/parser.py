"""Parse a Protocol Markdown file into a Protocol.

The step directive form `(context: steps 1, 2; model: small; turns: 4)` is fixed by ADR 0011.
Anything the parser does not understand is an error: a Protocol is process rails, so a line
that is silently dropped is a step or a guardrail that silently does not exist.

An Error handling bullet that starts `@tool fails:` is a FailureThreshold the runner enforces,
so it must read `escalate` or `retry once|twice|N times, then escalate`; other bullets are
text for the model.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from typing import Any

from cograil.domain import FailureThreshold, Protocol, Step
from cograil.errors import ProtocolParseError

_STEP = re.compile(r'^(\d+)\.\s+Step\s+"([^"]+)"\s*:\s*(.*)$')
_DIRECTIVE = re.compile(r"\(\s*((?:context|model|turns)\s*:[^()]*)\)\s*$")
_CONTEXT = re.compile(r"^steps?\s+(\d+(?:\s*,\s*\d+)*)$")
_TOOL_REF = re.compile(r"(?<![\w.])@(\w+(?:\.\w+)*)")
_KEY_VALUE = re.compile(r"^([A-Za-z][A-Za-z ]*?)\s*:\s*(.*)$")
_FAILS = re.compile(r"^@(\w+(?:\.\w+)*)\s+fails\s*:\s*(.*)$", re.IGNORECASE)
_ESCALATE = re.compile(
    r"^(?:retry\s+(?:(once)|(twice)|(\d+)\s+times?)\s*,?\s*then\s+)?escalate\b", re.IGNORECASE
)

_SECTIONS = {"error handling": "error_handling", "guardrails": "guardrails"}
_HEADERS = {
    "protocol",
    "description",
    "audience",
    "manual execution",
    "scheduled execution",
    "helpers",
}
_TIERS = ("small", "standard", "strong")


def parse_protocol(text: str, known_tools: Collection[str] | None = None) -> Protocol:
    """Parse Protocol Markdown. With known_tools, every @ref must name one of them."""
    header: dict[str, str] = {}
    steps: list[Step] = []
    sections: dict[str, list[str]] = {"error_handling": [], "guardrails": []}
    current: list[str] | None = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        if step_match := _STEP.match(line):
            current = None
            steps.append(_parse_step(step_match, lineno))
        elif line.startswith("- "):
            if current is None:
                raise ProtocolParseError(f"line {lineno}: bullet outside a section")
            current.append(line[2:].strip())
        else:
            key, value = _split_key_value(line, lineno)
            if key in _SECTIONS:
                if value:
                    raise ProtocolParseError(
                        f"line {lineno}: {key!r} takes bullets on the following lines, "
                        f"not text after the colon: {value!r}"
                    )
                current = sections[_SECTIONS[key]]
            elif steps:
                raise ProtocolParseError(f"line {lineno}: unexpected line after steps: {line!r}")
            elif key in header:
                raise ProtocolParseError(f"line {lineno}: duplicate header {key!r}")
            else:
                current = None
                header[key] = value
    protocol = _build(header, steps, sections)
    _check_tools(protocol, known_tools)
    return protocol


def _split_key_value(line: str, lineno: int) -> tuple[str, str]:
    match = _KEY_VALUE.match(line)
    if match is None:
        raise ProtocolParseError(f"line {lineno}: cannot parse {line!r}")
    key = match.group(1).lower()
    if key not in _HEADERS and key not in _SECTIONS:
        raise ProtocolParseError(f"line {lineno}: unknown header {match.group(1)!r}")
    return key, match.group(2).strip()


def _parse_step(match: re.Match[str], lineno: int) -> Step:
    number, name, body = int(match.group(1)), match.group(2), match.group(3)
    fields: dict[str, Any] = {}
    directive = _DIRECTIVE.search(body)
    if directive:
        fields = _parse_directive(directive.group(1), number)
        body = body[: directive.start()]
    instruction = body.strip()
    if not instruction:
        raise ProtocolParseError(f"line {lineno}: step {number} has no instruction")
    tools = list(dict.fromkeys(_TOOL_REF.findall(instruction)))
    return Step(number=number, name=name, instruction=instruction, tools=tools, **fields)


def _parse_directive(text: str, number: int) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for part in text.split(";"):
        key, _, value = part.partition(":")
        key, value = key.strip(), value.strip()
        if key in {"context", "model", "turns"} and not value:
            raise ProtocolParseError(f"step {number}: directive {key!r} has no value")
        if key == "context":
            context = _CONTEXT.match(value)
            if context is None:
                raise ProtocolParseError(f"step {number}: bad context {value!r}")
            fields["context_steps"] = [int(n) for n in context.group(1).split(",")]
        elif key == "model":
            if value not in _TIERS:
                raise ProtocolParseError(f"step {number}: unknown model tier {value!r}")
            fields["model_tier"] = value
        elif key == "turns":
            if not value.isdigit() or int(value) < 1:
                raise ProtocolParseError(f"step {number}: turns must be a positive integer")
            fields["max_turns"] = int(value)
        else:
            raise ProtocolParseError(f"step {number}: unknown directive {key!r}")
    return fields


def _build(header: dict[str, str], steps: list[Step], sections: dict[str, list[str]]) -> Protocol:
    if "protocol" not in header or not header["protocol"]:
        raise ProtocolParseError("missing 'Protocol:' header")
    if not steps:
        raise ProtocolParseError(f"protocol {header['protocol']!r} has no steps")
    if [s.number for s in steps] != list(range(1, len(steps) + 1)):
        raise ProtocolParseError("steps must be numbered 1, 2, 3, ... in order")
    fields: dict[str, Any] = {}
    if "audience" in header:
        fields["audiences"] = _csv(header["audience"])
    if "manual execution" in header:
        fields["manual_allowed"] = _allowed(header["manual execution"], "Manual execution")
    if "scheduled execution" in header:
        fields["scheduled_allowed"] = _allowed(header["scheduled execution"], "Scheduled execution")
    return Protocol(
        name=header["protocol"],
        description=header.get("description", ""),
        steps=steps,
        helpers=_csv(header.get("helpers", "")),
        error_handling=sections["error_handling"],
        failure_thresholds=_thresholds(sections["error_handling"]),
        guardrails=sections["guardrails"],
        **fields,
    )


def _thresholds(bullets: list[str]) -> list[FailureThreshold]:
    found: dict[str, FailureThreshold] = {}
    for bullet in bullets:
        fails = _FAILS.match(bullet)
        if fails is None:
            continue
        tool, action = fails.group(1), _ESCALATE.match(fails.group(2))
        if action is None:
            raise ProtocolParseError(
                f"error handling {bullet!r}: a '@tool fails:' bullet must say 'escalate' or "
                "'retry once|twice|N times, then escalate'"
            )
        if tool in found:
            raise ProtocolParseError(f"error handling: two '@{tool} fails:' bullets")
        once, twice, times = action.groups()
        retries = 1 if once else 2 if twice else int(times or 0)
        found[tool] = FailureThreshold(tool=tool, max_failures=retries + 1, rule=bullet)
    return list(found.values())


def _csv(value: str) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    return [] if items == ["none"] else items


def _allowed(value: str, label: str) -> bool:
    if value.lower() not in {"allowed", "not allowed"}:
        raise ProtocolParseError(f"{label} must be 'allowed' or 'not allowed', got {value!r}")
    return value.lower() == "allowed"


def _check_tools(protocol: Protocol, known_tools: Collection[str] | None) -> None:
    if known_tools is None:
        return
    unknown = [
        f"@{tool} (step {step.number})"
        for step in protocol.steps
        for tool in step.tools
        if tool not in known_tools
    ] + [
        f"@{threshold.tool} (error handling)"
        for threshold in protocol.failure_thresholds
        if threshold.tool not in known_tools
    ]
    if unknown:
        raise ProtocolParseError("unknown tool reference(s): " + ", ".join(unknown))

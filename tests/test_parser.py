"""Protocol parser tests: one per acceptance criterion of issue #6."""

from pathlib import Path

import pytest

from cograil.errors import ProtocolParseError
from cograil.parser import parse_protocol

LEAVE_REQUEST = Path(__file__).parents[1] / "workspaces/example-smb/protocols/leave_request.md"

HEADER = """Protocol: demo
Audience: managers, finance
Manual execution: not allowed
Scheduled execution: allowed
Helpers: triage, close_out
"""


def _with_step(step_line: str) -> str:
    return f"Protocol: demo\n\n{step_line}\n"


def test_valid_file_parses_into_protocol() -> None:
    protocol = parse_protocol(LEAVE_REQUEST.read_text())
    assert protocol.name == "leave_request"
    assert [s.name for s in protocol.steps] == [
        "Check balance",
        "Confirm dates",
        "Route and submit",
        "Notify",
    ]
    assert protocol.steps[2].tools == ["decide.approval_routing", "hris.submit_leave"]


def test_header_lines_become_fields() -> None:
    protocol = parse_protocol(HEADER + '\n1. Step "Only": Do it.\n')
    assert protocol.audiences == ["managers", "finance"]
    assert protocol.manual_allowed is False
    assert protocol.scheduled_allowed is True
    assert protocol.helpers == ["triage", "close_out"]


def test_example_header_defaults() -> None:
    protocol = parse_protocol(LEAVE_REQUEST.read_text())
    assert protocol.audiences == ["all-employees"]
    assert (protocol.manual_allowed, protocol.scheduled_allowed) == (True, False)
    assert protocol.helpers == []


def test_step_line_gives_number_name_instruction_and_tools() -> None:
    step = parse_protocol(LEAVE_REQUEST.read_text()).steps[3]
    assert step.number == 4
    assert step.name == "Notify"
    assert step.instruction.startswith("Use @notify.send to tell")
    assert step.tools == ["notify.send"]
    assert (step.context_steps, step.model_tier, step.max_turns) == (None, None, None)


def test_step_with_two_tools_keeps_order_and_drops_repeats() -> None:
    line = '1. Step "Both": Call @a.read, then @b.write, then @a.read again.'
    assert parse_protocol(_with_step(line)).steps[0].tools == ["a.read", "b.write"]


@pytest.mark.parametrize(
    ("directive", "context", "tier", "turns"),
    [
        ("(context: steps 1, 2; model: small; turns: 4)", [1, 2], "small", 4),
        ("(model: strong)", None, "strong", None),
        ("(context: step 1)", [1], None, None),
        ("(turns: 2)", None, None, 2),
    ],
)
def test_trailing_directive_sets_step_fields(
    directive: str, context: list[int] | None, tier: str | None, turns: int | None
) -> None:
    step = parse_protocol(_with_step(f'1. Step "A": Do it. {directive}')).steps[0]
    assert (step.context_steps, step.model_tier, step.max_turns) == (context, tier, turns)
    assert step.instruction == "Do it."


@pytest.mark.parametrize(
    "directive",
    ["(model: huge)", "(turns: 0)", "(turns: many)", "(context: all)", "(context: )"],
)
def test_bad_directive_is_an_error(directive: str) -> None:
    with pytest.raises(ProtocolParseError):
        parse_protocol(_with_step(f'1. Step "A": Do it. {directive}'))


def test_other_parenthesis_stays_in_instruction() -> None:
    step = parse_protocol(_with_step('1. Step "A": Ask (politely).')).steps[0]
    assert step.instruction == "Ask (politely)."


def test_error_handling_and_guardrails_are_lists() -> None:
    protocol = parse_protocol(LEAVE_REQUEST.read_text())
    assert len(protocol.error_handling) == 3
    assert protocol.error_handling[0].startswith("@hris.get_balance fails:")
    assert len(protocol.guardrails) == 3
    assert protocol.guardrails[0] == "Never submit leave for anyone other than the requester."


def test_unknown_tool_references_are_reported_by_name() -> None:
    known = {"hris.get_balance", "decide.approval_routing", "notify.send"}
    with pytest.raises(ProtocolParseError) as excinfo:
        parse_protocol(LEAVE_REQUEST.read_text(), known_tools=known)
    assert "@hris.submit_leave (step 3)" in str(excinfo.value)


def test_known_tools_accepts_the_example_protocol() -> None:
    known = {"hris.get_balance", "hris.submit_leave", "decide.approval_routing", "notify.send"}
    assert parse_protocol(LEAVE_REQUEST.read_text(), known_tools=known).name == "leave_request"


@pytest.mark.parametrize("section", ["Guardrails", "Error handling"])
def test_inline_section_text_is_an_error_not_silently_dropped(section: str) -> None:
    with pytest.raises(ProtocolParseError, match="not text after the colon"):
        parse_protocol(_with_step('1. Step "A": Do it.') + f"\n{section}: be kind\n")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("Protocol: demo\nProtocol: other\n" + '1. Step "A": Do it.', "duplicate header"),
        ("Protocol: demo\nAudience: a\nAudience: b\n" + '1. Step "A": Do it.', "duplicate header"),
        ('Protocol: demo\n1. Step "A": Do it.\nDescription: late', "unexpected line after steps"),
        ('Protocol: demo\n1. Step "A": Do it.\nHelpers: none', "unexpected line after steps"),
    ],
)
def test_header_ordering_error_branches(text: str, message: str) -> None:
    with pytest.raises(ProtocolParseError, match=message):
        parse_protocol(text)


def test_file_with_no_steps_is_an_error() -> None:
    with pytest.raises(ProtocolParseError, match="no steps"):
        parse_protocol(HEADER + "\nGuardrails:\n- Be kind.\n")


@pytest.mark.parametrize(
    "text",
    [
        '1. Step "A": Do it.',
        "Protocol: demo\nColour: red\n" + '1. Step "A": Do it.',
        "Protocol: demo\n- stray bullet\n" + '1. Step "A": Do it.',
        'Protocol: demo\n2. Step "A": Do it.',
        "Protocol: demo\nManual execution: sometimes\n" + '1. Step "A": Do it.',
    ],
)
def test_malformed_files_are_errors(text: str) -> None:
    with pytest.raises(ProtocolParseError):
        parse_protocol(text)


def _with_error_handling(*bullets: str) -> str:
    lines = "\n".join(f"- {bullet}" for bullet in bullets)
    return f'Protocol: demo\n\n1. Step "Look": Use @hris.get_balance.\n\nError handling:\n{lines}\n'


@pytest.mark.parametrize(
    ("bullet", "max_failures"),
    [
        ("@hris.get_balance fails: escalate.", 1),
        ("@hris.get_balance fails: retry once, then escalate to the Human Manager.", 2),
        ("@hris.get_balance fails: Retry twice then escalate", 3),
        ("@hris.get_balance fails: retry 4 times, then escalate.", 5),
    ],
)
def test_tool_fails_bullet_becomes_a_failure_threshold(bullet: str, max_failures: int) -> None:
    protocol = parse_protocol(_with_error_handling(bullet, "Balance too low: explain and stop."))
    (threshold,) = protocol.failure_thresholds
    assert (threshold.tool, threshold.max_failures, threshold.rule) == (
        "hris.get_balance",
        max_failures,
        bullet,
    )
    assert len(protocol.error_handling) == 2  # every bullet still reaches the model


@pytest.mark.parametrize(
    "bullets",
    [
        pytest.param(["@hris.get_balance fails: retry once and give up."], id="no-escalate"),
        pytest.param(
            ["@hris.get_balance fails: escalate", "@hris.get_balance fails: escalate"],
            id="duplicate",
        ),
    ],
)
def test_tool_fails_bullet_the_parser_cannot_enforce_is_an_error(bullets: list[str]) -> None:
    with pytest.raises(ProtocolParseError):
        parse_protocol(_with_error_handling(*bullets))


def test_failure_threshold_on_an_unknown_tool_is_reported() -> None:
    text = _with_error_handling("@hris.get_balans fails: escalate.")
    with pytest.raises(ProtocolParseError, match=r"@hris.get_balans \(error handling\)"):
        parse_protocol(text, known_tools={"hris.get_balance"})

"""Evals: a workspace's JSONL golden set run through the Runner at a chosen level (issue #30)."""

from cograil.evals.cases import EvalCase, load_cases
from cograil.evals.report import format_report, summarise
from cograil.evals.suite import CaseResult, Level, run_case, run_suite

__all__ = [
    "CaseResult",
    "EvalCase",
    "Level",
    "format_report",
    "load_cases",
    "run_case",
    "run_suite",
    "summarise",
]

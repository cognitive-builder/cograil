"""Principal identity: one spelling per principal.

Identity providers, the CLI and workspace files spell the same person differently
(`Alice@Example.com`, ` alice@example.com`). Gates compare principals to decide who may
approve, so every principal id is trimmed and lower-cased before it is stored or compared.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator


def normalise_principal_id(value: str) -> str:
    """The canonical spelling of a principal id: trimmed and lower-cased."""
    return value.strip().lower()


def same_principal(a: str, b: str) -> bool:
    """Whether two principal ids name the same principal, whatever their spelling."""
    return normalise_principal_id(a) == normalise_principal_id(b)


type PrincipalId = Annotated[str, AfterValidator(normalise_principal_id)]

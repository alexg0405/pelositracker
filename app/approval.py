"""One consistent operator-approval token for local execution controls."""
from __future__ import annotations

from typing import Any


APPROVAL_TOKEN = "approve"


def approval_granted(value: Any) -> bool:
    """Accept the shared token without making capitalization another burden."""
    return str(value or "").strip().casefold() == APPROVAL_TOKEN


def approval_instruction(action: str) -> str:
    return f'confirm the action with "{APPROVAL_TOKEN}" to {action}'

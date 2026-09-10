"""Explicit terminal outcomes shared by agent loops and their callers."""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class AgentResult:
    status: Literal["completed", "failed", "cancelled", "unverified", "limit_reached"]
    reason: str = ""

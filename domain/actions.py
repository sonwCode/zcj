from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class ActionParameter:
    key: str
    label: str
    type: str
    # Options may be plain strings or ``{"value": ..., "label": ...}``
    # records.  The latter lets platform actions expose configured provider
    # names without encoding labels into the submitted value.
    options: list[Any] = field(default_factory=list)


@dataclass(slots=True)
class PlatformAction:
    id: str
    label: str
    params: list[ActionParameter] = field(default_factory=list)
    sync: bool = False


@dataclass(slots=True)
class ActionExecutionCommand:
    platform: str
    account_id: int
    action_id: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ActionExecutionResult:
    ok: bool
    data: Any = None
    error: str = ""

# Copyright (c) 2025-2026 Kiss Benedek, operating as "GG4Ever" (https://github.com/ticksparancsnok/SMP)   
# License: GNU AFFERO GENERAL PUBLIC LICENSE Version 3
# Attribution required in modified versions: see NOTICE

"""Errors the UI can translate.

The API speaks English. Messages a user may see also carry a stable `code` (and the
values filled into the message as `params`), so the web UI can show them in the
user's language. An AppError answers

    {"detail": "<English message>", "code": "<code>", "params": {...}}

`detail` stays a plain string, so clients that only read `detail` keep working.
Codes are part of the API: add new ones freely, but don't rename existing ones
without updating WEBUI/src/i18n.
"""  # ruff: ignore[line-too-long]

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException


@dataclass(frozen=True)
class Problem:
    """A validation problem: stable code, English message, and the message's values."""  # ruff: ignore[line-too-long]

    code: str
    message: str
    params: dict[str, Any] = field(default_factory=dict[str, Any])

    def __str__(self) -> str:  # ruff: ignore[undocumented-magic-method]
        return self.message


class AppError(HTTPException):  # ruff: ignore[undocumented-public-class]
    def __init__(self, status_code: int, code: str, message: str, headers: dict[str, str] | None = None, **params: Any) -> None:  # ruff: ignore[any-type, line-too-long, undocumented-public-init]
        super().__init__(status_code, message, headers=headers)
        self.code = code
        self.params = params

    @classmethod
    def from_problem(cls, status_code: int, problem: Problem) -> AppError:  # ruff: ignore[undocumented-public-method]
        return cls(status_code, problem.code, problem.message, **problem.params)  # ruff: ignore[line-too-long]

    def body(self) -> dict[str, Any]:  # ruff: ignore[undocumented-public-method]
        return {"detail": self.detail, "code": self.code, "params": self.params}  # ruff: ignore[line-too-long]

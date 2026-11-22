# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""Errors the UI can translate.

The API speaks English. Messages a user may see also carry a stable `code` (and the
values filled into the message as `params`), so the web UI can show them in the
user's language. An AppError answers

    {"detail": "<English message>", "code": "<code>", "params": {...}}

`detail` stays a plain string, so clients that only read `detail` keep working.
Codes are part of the API: add new ones freely, but don't rename existing ones
without updating WEBUI/src/i18n.
"""

from dataclasses import dataclass, field
from typing import Any

from fastapi import HTTPException


@dataclass(frozen=True)
class Problem:
    """A validation problem: stable code, English message, and the message's values."""

    code: str
    message: str
    params: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


class AppError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, headers: dict[str, str] | None = None, **params: Any):
        super().__init__(status_code, message, headers=headers)
        self.code = code
        self.params = params

    @classmethod
    def from_problem(cls, status_code: int, problem: Problem) -> "AppError":
        return cls(status_code, problem.code, problem.message, **problem.params)

    def body(self) -> dict[str, Any]:
        return {"detail": self.detail, "code": self.code, "params": self.params}

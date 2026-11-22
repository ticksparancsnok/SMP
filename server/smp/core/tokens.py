# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""API token housekeeping."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from smp.core.models import ApiToken

if TYPE_CHECKING:
    from smp.core.context import Ctx


async def delete_tokens_of(ctx: Ctx, user_id: uuid.UUID) -> list[dict[str, Any]]:
    """Delete every API token the user created, including tokens created *by* those
    tokens (a token with core.tokens.manage can mint more; they would otherwise
    outlive the person behind them). Runs in the caller's transaction.

    Returns a non-sensitive description of what was deleted, for the audit log: id,
    name and permissions. Never the token, its hash or its prefix."""
    doomed: dict[uuid.UUID, ApiToken] = {}
    creators = {user_id}
    while creators:
        rows = await ctx.db.execute(ctx.select(ApiToken).where(ApiToken.created_by.in_(creators)))
        found = [t for t in rows.scalars() if t.id not in doomed]
        doomed.update((t.id, t) for t in found)
        creators = {t.id for t in found}
    for t in doomed.values():
        await ctx.db.delete(t)
    return [
        {
            "id": str(t.id),
            "name": t.name,
            "permissions": t.permissions,
            "created_by_token": t.created_by != user_id,
            "was_revoked": t.revoked_at is not None,
        }
        for t in sorted(doomed.values(), key=lambda t: t.created_at)
    ]

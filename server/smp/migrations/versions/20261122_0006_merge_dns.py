# SPDX-FileCopyrightText: 2026 GG4Ever (https://github.com/ticksparancsnok/SMP)
# SPDX-License-Identifier: AGPL-3.0-or-later
# Attribution required in modified versions: see NOTICE

"""merge the dns branch with the group_teachers/email branch

0004_dns and 0004_group_teachers were both written on top of 0003_otp, which
left two heads and made `smp migrate` (upgrade head) fail.

Revision ID: 0006_merge_dns
Revises: 0004_dns, 0005_email_unique
Create Date: 2026-11-22 12:00:00
"""

from collections.abc import Sequence

revision: str = '0006_merge_dns'
down_revision: str | Sequence[str] | None = ('0004_dns', '0005_email_unique')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

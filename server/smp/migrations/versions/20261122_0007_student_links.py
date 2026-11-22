"""disable short links created by students

Students no longer have shortlinks.links.create (links on the school's own domain
were a phishing vector). Links they already made would keep redirecting, so disable
those owned by users without a teacher or admin role. Disabled, not deleted: an
admin can still review them, or re-enable one if it was legitimate.

Revision ID: 0007_student_links
Revises: 0006_merge_dns
Create Date: 2026-11-22 13:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = '0007_student_links'
down_revision: str | Sequence[str] | None = '0006_merge_dns'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


DISABLE_STUDENT_LINKS = sa.text(
    """
    UPDATE shortlinks_links SET disabled = true
    WHERE disabled = false
      AND owner_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM role_assignments ra
        WHERE ra.user_id = shortlinks_links.owner_id AND ra.role IN ('admin', 'teacher')
      )
    """
)


def upgrade() -> None:
    op.execute(DISABLE_STUDENT_LINKS)


def downgrade() -> None:
    pass  # which links were disabled by this migration isn't recorded; re-enable by hand

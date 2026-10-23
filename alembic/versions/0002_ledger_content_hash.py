"""ledger content hash

Adds message_ledger.content_hash: the SHA-256 of the text each message had when
it was handled, so an edit update that leaves the text unchanged (a reaction, a
view count) is not processed again, and edits made while the worker was offline
are found by catch-up (Phase 0.4).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23 17:13:28.806460

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('message_ledger', schema=None) as batch_op:
        batch_op.add_column(sa.Column('content_hash', sa.String(length=64), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('message_ledger', schema=None) as batch_op:
        batch_op.drop_column('content_hash')

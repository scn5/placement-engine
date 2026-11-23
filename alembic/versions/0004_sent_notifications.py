"""sent notifications

Adds sent_notifications table to track dispatched push alerts across
API restarts (Phase 1.4, step 2).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-24 01:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'sent_notifications',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('notification_type', sa.String(length=32), nullable=False),
        sa.Column('event_id', sa.Integer(), nullable=True),
        sa.Column('role_track_id', sa.Integer(), nullable=True),
        sa.Column('window', sa.String(length=16), nullable=True),
        sa.Column('sent_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['event_id'],
            ['timeline_events.id'],
            name=op.f('fk_sent_notifications_event_id_timeline_events'),
            ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['role_track_id'],
            ['role_tracks.id'],
            name=op.f('fk_sent_notifications_role_track_id_role_tracks'),
            ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_sent_notifications')),
        sa.UniqueConstraint('event_id', 'window', name=op.f('uq_sent_notifications_event_id_window')),
        sa.UniqueConstraint('role_track_id', 'notification_type', name=op.f('uq_sent_notifications_role_track_id_notification_type')),
    )
    with op.batch_alter_table('sent_notifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sent_notifications_id'), ['id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('sent_notifications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sent_notifications_id'))
    op.drop_table('sent_notifications')

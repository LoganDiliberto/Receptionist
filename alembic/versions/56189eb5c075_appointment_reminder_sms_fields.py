"""appointment reminder sms fields

Revision ID: 56189eb5c075
Revises: 89b29a9a65a1
Create Date: 2026-07-27 19:46:49.877808

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '56189eb5c075'
down_revision: Union[str, None] = '89b29a9a65a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('appointments', schema=None) as batch_op:
        # server_default so existing appointment rows get 'pending' when the
        # non-nullable column is added; the ORM default covers new inserts.
        batch_op.add_column(sa.Column(
            'reminder_status', sa.String(), nullable=False, server_default='pending'
        ))
        batch_op.add_column(sa.Column('reminder_sent_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('reminder_error', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('appointments', schema=None) as batch_op:
        batch_op.drop_column('reminder_error')
        batch_op.drop_column('reminder_sent_at')
        batch_op.drop_column('reminder_status')

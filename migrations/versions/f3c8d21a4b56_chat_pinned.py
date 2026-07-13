"""chat pinned flag

Revision ID: f3c8d21a4b56
Revises: e7b2c9a10f34
Create Date: 2026-07-13 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'f3c8d21a4b56'
down_revision = 'e7b2c9a10f34'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('chat', schema=None) as batch_op:
        batch_op.add_column(sa.Column('pinned', sa.Boolean(), nullable=False,
                                      server_default=sa.false()))


def downgrade():
    with op.batch_alter_table('chat', schema=None) as batch_op:
        batch_op.drop_column('pinned')

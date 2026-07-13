"""user role and active for RBAC

Revision ID: d4e1a7c2b900
Revises: a176563999d5
Create Date: 2026-07-12 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd4e1a7c2b900'
down_revision = 'a176563999d5'
branch_labels = None
depends_on = None


def upgrade():
    # NOT NULL columns on a populated table need a server_default so
    # existing rows get a value; the app-side default handles new rows.
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('role', sa.String(length=16),
                                      nullable=False, server_default='user'))
        batch_op.add_column(sa.Column('active', sa.Boolean(),
                                      nullable=False, server_default=sa.true()))


def downgrade():
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_column('active')
        batch_op.drop_column('role')

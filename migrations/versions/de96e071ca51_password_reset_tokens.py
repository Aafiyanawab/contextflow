"""password reset tokens

Creates the password_reset_token table backing the Forgot Password flow:
a single-use, expiring grant that stores ONLY the sha256 hash of the emailed
token (never the raw token). Additive CREATE TABLE — no changes to existing
tables, so existing rows are untouched.

Revision ID: de96e071ca51
Revises: 29e3774fbabb
Create Date: 2026-07-20 21:58:41.308261

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'de96e071ca51'
down_revision = '29e3774fbabb'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'password_reset_token',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('user_id', sa.String(length=32), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['user.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('token_hash'),
    )
    with op.batch_alter_table('password_reset_token', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_password_reset_token_user_id'), ['user_id'], unique=False)


def downgrade():
    with op.batch_alter_table('password_reset_token', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_password_reset_token_user_id'))
    op.drop_table('password_reset_token')

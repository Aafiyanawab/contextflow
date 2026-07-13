"""companies, audit log, three-role RBAC

Revision ID: e7b2c9a10f34
Revises: d4e1a7c2b900
Create Date: 2026-07-12 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'e7b2c9a10f34'
down_revision = 'd4e1a7c2b900'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'company',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=120), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name'),
    )
    op.create_table(
        'audit_log',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('actor_id', sa.String(length=32), nullable=True),
        sa.Column('actor_email', sa.String(length=254), nullable=True),
        sa.Column('action', sa.String(length=40), nullable=False),
        sa.Column('target', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.add_column(sa.Column('company_name', sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column('company_id', sa.String(length=32), nullable=True))
        batch_op.alter_column('role', existing_type=sa.String(length=16),
                              type_=sa.String(length=20))
        batch_op.create_foreign_key('fk_user_company', 'company', ['company_id'], ['id'])

    # Migrate existing role vocabulary: admin -> super_admin, user -> employee.
    user = sa.table('user', sa.column('role', sa.String))
    op.execute(user.update().where(user.c.role == 'admin').values(role='super_admin'))
    op.execute(user.update().where(user.c.role == 'user').values(role='employee'))


def downgrade():
    op.execute("UPDATE user SET role='admin' WHERE role IN ('super_admin','company_admin')")
    op.execute("UPDATE user SET role='user' WHERE role='employee'")
    with op.batch_alter_table('user', schema=None) as batch_op:
        batch_op.drop_constraint('fk_user_company', type_='foreignkey')
        batch_op.alter_column('role', existing_type=sa.String(length=20),
                              type_=sa.String(length=16))
        batch_op.drop_column('company_id')
        batch_op.drop_column('company_name')
    op.drop_table('audit_log')
    op.drop_table('company')

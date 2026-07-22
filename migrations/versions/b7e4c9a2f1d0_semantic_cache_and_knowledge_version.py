"""semantic cache + workspace knowledge_version + message cache accounting

Additive schema for the Semantic Cache feature:

  * NEW TABLE semantic_cache_entry — one cached question→answer pair per
    workspace (question text, packed question embedding, response, repo
    context snapshot, knowledge_version stamp, token accounting, TTL).
  * workspace.knowledge_version — monotonic "knowledge changed" counter;
    a bump makes every prior cache entry for that workspace stale.
  * message.cache_hit / message.response_ms — per-answer accounting that
    feeds the AI Diagnostics cache cards (hit rate, cost saved, latency).

All changes are additive; existing rows get server defaults so nothing
is rewritten. No existing table's columns are altered or dropped.

Revision ID: b7e4c9a2f1d0
Revises: de96e071ca51
Create Date: 2026-07-22 06:10:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e4c9a2f1d0'
down_revision = 'de96e071ca51'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'semantic_cache_entry',
        sa.Column('id', sa.String(length=32), nullable=False),
        sa.Column('workspace_id', sa.String(length=32), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('question_embedding', sa.LargeBinary(), nullable=False),
        sa.Column('response', sa.Text(), nullable=False),
        sa.Column('repo_context', sa.JSON(), nullable=False),
        sa.Column('knowledge_version', sa.Integer(), nullable=False),
        sa.Column('model', sa.String(length=40), nullable=True),
        sa.Column('tokens_in', sa.Integer(), nullable=False),
        sa.Column('tokens_out', sa.Integer(), nullable=False),
        sa.Column('hit_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['workspace_id'], ['workspace.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('semantic_cache_entry', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_semantic_cache_entry_workspace_id'),
            ['workspace_id'], unique=False)
        batch_op.create_index(
            batch_op.f('ix_semantic_cache_entry_expires_at'),
            ['expires_at'], unique=False)

    # workspace.knowledge_version — default 1 for every existing workspace.
    with op.batch_alter_table('workspace', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'knowledge_version', sa.Integer(), nullable=False,
            server_default=sa.text('1')))

    # message cache accounting — existing rows are cache misses at 0 ms.
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'cache_hit', sa.Boolean(), nullable=False,
            server_default=sa.false()))
        batch_op.add_column(sa.Column(
            'response_ms', sa.Integer(), nullable=False,
            server_default=sa.text('0')))


def downgrade():
    with op.batch_alter_table('message', schema=None) as batch_op:
        batch_op.drop_column('response_ms')
        batch_op.drop_column('cache_hit')

    with op.batch_alter_table('workspace', schema=None) as batch_op:
        batch_op.drop_column('knowledge_version')

    with op.batch_alter_table('semantic_cache_entry', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_semantic_cache_entry_expires_at'))
        batch_op.drop_index(batch_op.f('ix_semantic_cache_entry_workspace_id'))
    op.drop_table('semantic_cache_entry')

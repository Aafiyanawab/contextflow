"""repo sync: document path identity + sync baseline

Adds the schema the Repository Synchronization Engine needs:

  * knowledge_source.last_synced_commit_sha / default_branch
        the head-commit-guard baseline for incremental sync.
  * document.repo_path / document.blob_sha
        repo-file identity (path) + the per-file change signal (git blob SHA).

and swaps the Document identity constraint from content-hash
(workspace_id, sha256) to path (source_id, repo_path) — so two repo files
with identical bytes at different paths are distinct rows. Uploads keep
repo_path NULL (NULLs are distinct under a unique constraint, so they are
unaffected; their dedupe is the app-level sha256 check).

All new columns are nullable, so existing rows migrate untouched — repos
connected before this feature get their baseline backfilled on their next
sync (a one-time full re-index), not here.

The constraint swap is dialect-aware: an in-place ALTER on PostgreSQL (so
the inbound FK chunk.document_id -> document.id is never dropped), and a
batch table-recreate on SQLite (which has no ALTER for constraints). The
old constraint is unnamed, so on PostgreSQL we look its name up, and on
SQLite we supply a naming convention to reference it.

Revision ID: 29e3774fbabb
Revises: f3c8d21a4b56
Create Date: 2026-07-19 01:38:10.321710

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '29e3774fbabb'
down_revision = 'f3c8d21a4b56'
branch_labels = None
depends_on = None

# Names the unnamed unique constraint by its first column during SQLite batch
# reflection, so it can be referenced for dropping.
_SQLITE_NAMING = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def upgrade():
    bind = op.get_bind()
    dialect = bind.dialect.name

    # 1) knowledge_source: incremental-sync baseline columns (additive, nullable).
    with op.batch_alter_table("knowledge_source", schema=None) as batch_op:
        batch_op.add_column(sa.Column("last_synced_commit_sha", sa.String(length=40), nullable=True))
        batch_op.add_column(sa.Column("default_branch", sa.String(length=120), nullable=True))

    # 2) document: repo-file identity/change columns + identity constraint swap.
    if dialect == "postgresql":
        with op.batch_alter_table("document", schema=None) as batch_op:
            batch_op.add_column(sa.Column("repo_path", sa.String(length=300), nullable=True))
            batch_op.add_column(sa.Column("blob_sha", sa.String(length=40), nullable=True))
        # Drop the old unique by its real (auto-generated) name, looked up
        # rather than hardcoded. Before this migration there is exactly one
        # unique constraint on `document` — the (workspace_id, sha256) one.
        old = bind.execute(sa.text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'document'::regclass AND contype = 'u'")).scalar()
        if old:
            op.drop_constraint(old, "document", type_="unique")
        op.create_unique_constraint(
            "uq_document_source_id_repo_path", "document", ["source_id", "repo_path"])
    else:
        with op.batch_alter_table(
                "document", schema=None, naming_convention=_SQLITE_NAMING) as batch_op:
            batch_op.add_column(sa.Column("repo_path", sa.String(length=300), nullable=True))
            batch_op.add_column(sa.Column("blob_sha", sa.String(length=40), nullable=True))
            batch_op.drop_constraint("uq_document_workspace_id", type_="unique")
            batch_op.create_unique_constraint(
                "uq_document_source_id_repo_path", ["source_id", "repo_path"])


def downgrade():
    # Reverse of upgrade. Note: re-adding the (workspace_id, sha256) unique
    # will fail if repo files with identical content were indexed under the new
    # schema — that is inherent to reverting a relaxed constraint, not a bug.
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Restore the old unique, named exactly what the upgrade's SQLite naming
    # convention (uq_%(table_name)s_%(column_0_name)s) assigns to the original
    # unnamed constraint. That single name lets a later upgrade's drop match
    # both the prod state (originally unnamed -> convention-named on reflection)
    # and this restored one — keeping upgrade/downgrade symmetric.
    if dialect == "postgresql":
        op.drop_constraint("uq_document_source_id_repo_path", "document", type_="unique")
        op.create_unique_constraint("uq_document_workspace_id", "document", ["workspace_id", "sha256"])
        with op.batch_alter_table("document", schema=None) as batch_op:
            batch_op.drop_column("blob_sha")
            batch_op.drop_column("repo_path")
    else:
        with op.batch_alter_table("document", schema=None) as batch_op:
            batch_op.drop_constraint("uq_document_source_id_repo_path", type_="unique")
            batch_op.create_unique_constraint("uq_document_workspace_id", ["workspace_id", "sha256"])
            batch_op.drop_column("blob_sha")
            batch_op.drop_column("repo_path")

    with op.batch_alter_table("knowledge_source", schema=None) as batch_op:
        batch_op.drop_column("default_branch")
        batch_op.drop_column("last_synced_commit_sha")

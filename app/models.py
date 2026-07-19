import uuid
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _uuid():
    return uuid.uuid4().hex


def utcnow():
    return datetime.now(timezone.utc)


class User(db.Model):
    """An authenticated person. Owns workspaces; identities hold OAuth
    provider links (GitHub, Google). password_hash is set only for
    email/password accounts — OAuth-only users have none."""
    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    name = db.Column(db.String(120))
    email = db.Column(db.String(254), unique=True)
    password_hash = db.Column(db.String(255))  # null for OAuth-only accounts
    avatar_url = db.Column(db.String(300))
    company_name = db.Column(db.String(120))  # free-text, self-entered on profile
    company_id = db.Column(db.String(32), db.ForeignKey("company.id"))
    # RBAC: "super_admin" | "company_admin" | "employee". Assigned via the
    # admin dashboard or `flask set-admin`; role decides where a login lands
    # and what the admin dashboard scopes to.
    role = db.Column(db.String(20), nullable=False, default="employee")
    # Soft-disable: a deactivated account can't establish a session.
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    last_login_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    @property
    def is_super_admin(self):
        return self.role == "super_admin"

    @property
    def is_company_admin(self):
        return self.role == "company_admin"

    @property
    def is_admin(self):
        """Anyone who may open the admin dashboard."""
        return self.role in ("super_admin", "company_admin")

    @property
    def auth_provider(self):
        if self.identities:
            return self.identities[0].provider.capitalize()
        return "Email" if self.password_hash else "—"

    identities = db.relationship("OAuthIdentity", backref="user",
                                 cascade="all, delete-orphan")
    workspaces = db.relationship("Workspace", backref="owner",
                                 cascade="all, delete-orphan")


class Company(db.Model):
    """An organization. Company Admins manage users within their own
    company; Super Admins manage every company."""
    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    name = db.Column(db.String(120), nullable=False, unique=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    members = db.relationship("User", backref="company")


class AuditLog(db.Model):
    """Append-only record of privileged admin actions (role changes,
    (de)activations, company management). actor_* are denormalized so
    the trail survives deletion of the acting user."""
    __tablename__ = "audit_log"
    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    actor_id = db.Column(db.String(32))
    actor_email = db.Column(db.String(254))
    action = db.Column(db.String(40), nullable=False)
    target = db.Column(db.String(200))
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)


class OAuthIdentity(db.Model):
    """One external login for a user. A second provider (e.g. Google)
    is just another row linked to the same user — no schema change."""
    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey("user.id"), nullable=False)
    provider = db.Column(db.String(20), nullable=False)      # "github" | "google"
    provider_uid = db.Column(db.String(64), nullable=False)  # provider's user id
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    __table_args__ = (db.UniqueConstraint("provider", "provider_uid"),)


class Workspace(db.Model):
    """A knowledge container. Sources (GitHub repos, uploaded documents)
    hang off it; so do chats and, later, capsules. The v1 repo_url /
    discovered_context columns moved into KnowledgeSource rows."""
    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    user_id = db.Column(db.String(32), db.ForeignKey("user.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    chats = db.relationship(
        "Chat",
        backref="workspace",
        cascade="all, delete-orphan",
        order_by="Chat.updated_at.desc()",
    )
    sources = db.relationship(
        "KnowledgeSource",
        backref="workspace",
        cascade="all, delete-orphan",
        order_by="KnowledgeSource.created_at.asc()",
    )
    capsules = db.relationship(
        "Capsule",
        backref="workspace",
        cascade="all, delete-orphan",
    )

    @property
    def github_source(self):
        """The workspace's GitHub source, if any. v1 workspaces have
        exactly one; v2 workspaces may have zero or several sources."""
        return next((s for s in self.sources if s.type == "github"), None)

    @property
    def context_profile(self):
        """Discovered repo profile (cloud/IaC/CI/…) for prompt building
        and the overview grid. Empty dict for non-GitHub workspaces."""
        src = self.github_source
        return (src.profile or {}) if src else {}


class KnowledgeSource(db.Model):
    """One connected knowledge origin inside a workspace: a GitHub
    repository now; uploaded documents from Increment 2. `profile` is
    adapter-specific enrichment (the GitHub discovery grid); ingestion
    status/errors live here so the UI has one place to look."""
    __tablename__ = "knowledge_source"
    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    workspace_id = db.Column(db.String(32), db.ForeignKey("workspace.id"),
                             nullable=False)
    type = db.Column(db.String(16), nullable=False)  # "github" | "upload"
    name = db.Column(db.String(120), nullable=False)
    uri = db.Column(db.String(300))  # repo URL; null for uploads
    status = db.Column(db.String(16), nullable=False, default="ready")
    # pending | ingesting | ready | error
    profile = db.Column(db.JSON, nullable=False, default=dict)
    error = db.Column(db.Text)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    last_ingested_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    # Incremental-sync baseline (repo sources): the git commit we last synced
    # to — the head-commit guard compares against it — and the branch whose
    # head we track. Both null until the first sync records them.
    last_synced_commit_sha = db.Column(db.String(40))
    default_branch = db.Column(db.String(120))

    # The same repo can't be connected twice to one workspace.
    __table_args__ = (db.UniqueConstraint("workspace_id", "uri"),)

    documents = db.relationship("Document", backref="source",
                                cascade="all, delete-orphan")


class Document(db.Model):
    """One file inside a source (an uploaded file, or a repo file).
    Identity is per source type: a repo file is identified by its repo_path
    (so README.md and docs/README.md stay distinct even with byte-identical
    content); uploads have repo_path NULL and dedupe on content in app code.
    sha256 stays for integrity / upload dedupe; blob_sha (the git blob SHA)
    is the per-file change signal the incremental sync engine diffs."""
    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    source_id = db.Column(db.String(32), db.ForeignKey("knowledge_source.id"),
                          nullable=False)
    workspace_id = db.Column(db.String(32), db.ForeignKey("workspace.id"),
                             nullable=False)
    filename = db.Column(db.String(300), nullable=False)  # repo path or upload name — display/attribution
    # Repo-file identity + change tracking (both null for uploads):
    repo_path = db.Column(db.String(300))  # repo-relative path; the identity key for github docs
    blob_sha = db.Column(db.String(40))    # git blob SHA; per-file change signal for incremental sync
    mime = db.Column(db.String(100))
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    sha256 = db.Column(db.String(64), nullable=False)  # content hash — integrity + upload dedupe
    storage_key = db.Column(db.String(120))  # original file, via app/storage.py
    text = db.Column(db.Text)  # extraction result; chunks derive from this
    meta = db.Column(db.JSON, nullable=False, default=dict)  # pages, …
    status = db.Column(db.String(16), nullable=False, default="pending")
    # pending | extracted | chunked | embedded | error
    error = db.Column(db.Text)  # user-safe extraction failure reason
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    # Repo files are identified by path, not content, so two files with
    # identical bytes at different paths are distinct rows. Uploads keep
    # repo_path NULL — NULLs are distinct under a unique constraint, so this
    # never constrains them (their dedupe is the app-level sha256 check).
    __table_args__ = (db.UniqueConstraint("source_id", "repo_path",
                                          name="uq_document_source_id_repo_path"),)

    chunks = db.relationship("Chunk", backref="document",
                             cascade="all, delete-orphan")


class Chunk(db.Model):
    """One retrieval unit: ~500 tokens of text plus its structural
    metadata and (from Increment 3) its embedding. workspace_id is
    denormalized so scoped vector search never joins."""
    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    document_id = db.Column(db.String(32), db.ForeignKey("document.id"),
                            nullable=False)
    workspace_id = db.Column(db.String(32), db.ForeignKey("workspace.id"),
                             nullable=False, index=True)
    seq = db.Column(db.Integer, nullable=False, default=0)
    text = db.Column(db.Text, nullable=False, default="")
    token_count = db.Column(db.Integer, nullable=False, default=0)
    meta = db.Column(db.JSON, nullable=False, default=dict)
    embedding = db.Column(db.LargeBinary)  # packed float32; null until embedded
    embedding_model = db.Column(db.String(40))

    # Deleting a chunk (e.g. disconnecting the repo it came from) must also
    # drop its capsule memberships — otherwise the capsule_chunk → chunk FK
    # blocks the delete on an enforcing DB. Capsules themselves survive; a
    # rebuild prunes any emptied ones. Chats never reference chunks, so this
    # keeps conversations fully independent of knowledge sources.
    capsule_links = db.relationship("CapsuleChunk", backref="chunk",
                                    cascade="all, delete-orphan")


class Capsule(db.Model):
    """An automatically maintained knowledge domain: routing index always,
    injectable summary when the query warrants it. Built from Increment 5;
    schema lands now so message snapshots can reference capsule ids."""
    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    workspace_id = db.Column(db.String(32), db.ForeignKey("workspace.id"),
                             nullable=False, index=True)
    slug = db.Column(db.String(80), nullable=False)
    title = db.Column(db.String(160), nullable=False)
    keywords = db.Column(db.JSON, nullable=False, default=list)
    concepts = db.Column(db.JSON, nullable=False, default=list)
    summary = db.Column(db.Text, nullable=False, default="")
    related = db.Column(db.JSON, nullable=False, default=list)  # capsule ids
    centroid = db.Column(db.LargeBinary)  # packed float32, for routing
    token_count = db.Column(db.Integer, nullable=False, default=0)
    status = db.Column(db.String(16), nullable=False, default="fresh")
    # fresh | stale | building
    version = db.Column(db.Integer, nullable=False, default=1)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    __table_args__ = (db.UniqueConstraint("workspace_id", "slug"),)

    memberships = db.relationship("CapsuleChunk", backref="capsule",
                                  cascade="all, delete-orphan")


class CapsuleChunk(db.Model):
    """Capsule membership. A chunk belongs to at most one capsule today,
    but the link table keeps that a policy, not a schema constraint."""
    __tablename__ = "capsule_chunk"
    capsule_id = db.Column(db.String(32), db.ForeignKey("capsule.id"),
                           primary_key=True)
    chunk_id = db.Column(db.String(32), db.ForeignKey("chunk.id"),
                         primary_key=True)


class Chat(db.Model):
    """One line of inquiry inside a workspace. Contributes conversation
    history to prompts; context always comes from the workspace."""
    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    workspace_id = db.Column(db.String(32), db.ForeignKey("workspace.id"), nullable=False)
    title = db.Column(db.String(160), nullable=False, default="New chat")
    pinned = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    messages = db.relationship(
        "Message",
        backref="chat",
        cascade="all, delete-orphan",
        order_by="Message.created_at.asc()",
    )


class Message(db.Model):
    """A single turn. Assistant messages store a snapshot of the orchestration
    (intent, injected/withheld context, tokens) so the Context Inspector stays
    accurate even after a workspace rescan changes the live context."""
    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    chat_id = db.Column(db.String(32), db.ForeignKey("chat.id"), nullable=False)
    role = db.Column(db.String(12), nullable=False)  # user | assistant
    content = db.Column(db.Text, nullable=False, default="")

    # Orchestration snapshot — assistant messages only
    intent = db.Column(db.String(24))
    method = db.Column(db.String(16))  # rule-based | keywords | semantic | global-fallback
    matched_keywords = db.Column(db.JSON)
    injected_context = db.Column(db.JSON)  # profile keys (github workspaces)
    withheld_context = db.Column(db.JSON)
    tokens_in = db.Column(db.Integer, nullable=False, default=0)
    tokens_out = db.Column(db.Integer, nullable=False, default=0)

    # Capsule-routing snapshot (Increment 6) — frozen per exchange so the
    # Inspector stays truthful after later rescans/re-clustering.
    capsules_used = db.Column(db.JSON)      # [{title, summary_injected, tokens}]
    capsules_withheld = db.Column(db.JSON)  # [{title, reason}]
    chunks_used = db.Column(db.JSON)        # [{filename, page, tokens, score}]
    context_tokens = db.Column(db.Integer, nullable=False, default=0)  # actually sent
    naive_tokens = db.Column(db.Integer, nullable=False, default=0)    # naive-RAG baseline

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

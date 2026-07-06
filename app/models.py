import uuid
from datetime import datetime, timezone

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def _uuid():
    return uuid.uuid4().hex


def utcnow():
    return datetime.now(timezone.utc)


class User(db.Model):
    """An authenticated person. Owns workspaces; identities hold the
    OAuth provider links (GitHub now, Google later)."""
    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    name = db.Column(db.String(120))
    email = db.Column(db.String(254), unique=True)
    avatar_url = db.Column(db.String(300))
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)
    last_login_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    identities = db.relationship("OAuthIdentity", backref="user",
                                 cascade="all, delete-orphan")
    workspaces = db.relationship("Workspace", backref="owner",
                                 cascade="all, delete-orphan")


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

    # The same repo can't be connected twice to one workspace.
    __table_args__ = (db.UniqueConstraint("workspace_id", "uri"),)

    documents = db.relationship("Document", backref="source",
                                cascade="all, delete-orphan")


class Document(db.Model):
    """One file inside a source (an uploaded file, or later a repo file).
    sha256 dedupes re-uploads per workspace. Populated from Increment 2;
    the table exists now so migrations stay linear."""
    id = db.Column(db.String(32), primary_key=True, default=_uuid)
    source_id = db.Column(db.String(32), db.ForeignKey("knowledge_source.id"),
                          nullable=False)
    workspace_id = db.Column(db.String(32), db.ForeignKey("workspace.id"),
                             nullable=False)
    filename = db.Column(db.String(300), nullable=False)
    mime = db.Column(db.String(100))
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    sha256 = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(16), nullable=False, default="pending")
    # pending | extracted | chunked | embedded | error
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

    __table_args__ = (db.UniqueConstraint("workspace_id", "sha256"),)

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
    method = db.Column(db.String(16))  # rule-based | openai
    matched_keywords = db.Column(db.JSON)
    injected_context = db.Column(db.JSON)
    withheld_context = db.Column(db.JSON)
    tokens_in = db.Column(db.Integer, nullable=False, default=0)
    tokens_out = db.Column(db.Integer, nullable=False, default=0)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow)

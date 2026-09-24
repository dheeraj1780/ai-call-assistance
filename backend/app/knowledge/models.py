"""Company knowledge base. Tenant-owned, RLS-protected. Uploaded files are NOT stored: text
is extracted at upload time, chunked, and only the chunks (plus embeddings) are kept."""

import enum
import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.ai.embeddings import EMBEDDING_DIM
from app.common.models import (
    Base,
    CreatedAtMixin,
    TenantScopedMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
    sql_in,
)


class DocumentStatus(enum.StrEnum):
    UPLOADED = "UPLOADED"
    PROCESSING = "PROCESSING"
    READY = "READY"
    FAILED = "FAILED"


class KnowledgeDocument(UUIDPrimaryKeyMixin, TenantScopedMixin, TimestampMixin, Base):
    __tablename__ = "knowledge_documents"
    __table_args__ = (
        CheckConstraint(f"status IN ({sql_in(DocumentStatus)})", name="status_valid"),
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
        CheckConstraint("version >= 1", name="version_positive"),
        UniqueConstraint("company_id", "id"),
        ForeignKeyConstraint(
            ["company_id", "uploaded_by_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (uploaded_by_user_id)",
            name="fk_knowledge_documents_uploader_member",
        ),
        Index("ix_knowledge_documents_company_id_created_at", "company_id", "created_at"),
    )

    title: Mapped[str] = mapped_column(String(200), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="upload")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding_model: Mapped[str | None] = mapped_column(String(64))
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class KnowledgeChunk(UUIDPrimaryKeyMixin, TenantScopedMixin, CreatedAtMixin, Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("company_id", "id"),
        UniqueConstraint("document_id", "ordinal"),
        ForeignKeyConstraint(
            ["company_id", "document_id"],
            ["knowledge_documents.company_id", "knowledge_documents.id"],
            ondelete="CASCADE",
            name="fk_knowledge_chunks_document",
        ),
        Index("ix_knowledge_chunks_company_id_document_id", "company_id", "document_id"),
        Index(
            "ix_knowledge_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))

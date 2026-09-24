"""Company knowledge base: documents + chunks with pgvector embeddings (HNSW, cosine).

Revision ID: 0006_knowledge
Revises: 0005_realtime
Create Date: 2026-09-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0006_knowledge"
down_revision: str | None = "0005_realtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)
NOW = sa.text("now()")


def enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        "USING (company_id = app_current_company_id()) "
        "WITH CHECK (company_id = app_current_company_id())"
    )


def upgrade() -> None:
    op.create_table(
        "knowledge_documents",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("chunk_count", sa.Integer, nullable=False),
        sa.Column("embedding_model", sa.String(64)),
        sa.Column("uploaded_by_user_id", UUID),
        sa.Column("processed_at", TS),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.Column("updated_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_documents"),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE",
            name="fk_knowledge_documents_company_id_companies",
        ),
        sa.UniqueConstraint("company_id", "id", name="uq_knowledge_documents_company_id_id"),
        sa.ForeignKeyConstraint(
            ["company_id", "uploaded_by_user_id"],
            ["company_members.company_id", "company_members.user_id"],
            ondelete="SET NULL (uploaded_by_user_id)",
            name="fk_knowledge_documents_uploader_member",
        ),
        sa.CheckConstraint(
            "status IN ('UPLOADED', 'PROCESSING', 'READY', 'FAILED')",
            name="ck_knowledge_documents_status_valid",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_knowledge_documents_size_non_negative"),
        sa.CheckConstraint("version >= 1", name="ck_knowledge_documents_version_positive"),
    )
    op.create_index(
        "ix_knowledge_documents_company_id_created_at", "knowledge_documents",
        ["company_id", "created_at"],
    )
    enable_rls("knowledge_documents")

    op.create_table(
        "knowledge_chunks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("company_id", UUID, nullable=False),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("embedding", Vector(1024)),
        sa.Column("created_at", TS, nullable=False, server_default=NOW),
        sa.PrimaryKeyConstraint("id", name="pk_knowledge_chunks"),
        sa.ForeignKeyConstraint(
            ["company_id"], ["companies.id"], ondelete="CASCADE",
            name="fk_knowledge_chunks_company_id_companies",
        ),
        sa.UniqueConstraint("company_id", "id", name="uq_knowledge_chunks_company_id_id"),
        sa.UniqueConstraint("document_id", "ordinal", name="uq_knowledge_chunks_document_id_ordinal"),
        sa.ForeignKeyConstraint(
            ["company_id", "document_id"],
            ["knowledge_documents.company_id", "knowledge_documents.id"],
            ondelete="CASCADE", name="fk_knowledge_chunks_document",
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_company_id_document_id", "knowledge_chunks",
        ["company_id", "document_id"],
    )
    op.create_index(
        "ix_knowledge_chunks_embedding_hnsw", "knowledge_chunks", ["embedding"],
        postgresql_using="hnsw", postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    enable_rls("knowledge_chunks")


def downgrade() -> None:
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")

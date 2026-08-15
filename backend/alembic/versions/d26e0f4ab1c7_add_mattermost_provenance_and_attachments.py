"""add Mattermost provenance and attachments

Revision ID: d26e0f4ab1c7
Revises: c83d1e7a4b6f
Create Date: 2026-08-14
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d26e0f4ab1c7"
down_revision: str | None = "c83d1e7a4b6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mattermost_event_state",
        sa.Column("root_post_id", sa.String(), nullable=True),
    )
    op.add_column(
        "mattermost_event_state",
        sa.Column("source_user_id", sa.String(), nullable=True),
    )
    op.add_column(
        "mattermost_event_state",
        sa.Column("source_username", sa.String(), nullable=True),
    )
    op.add_column(
        "mattermost_event_state",
        sa.Column("source_display_name", sa.String(), nullable=True),
    )
    op.add_column(
        "mattermost_event_state",
        sa.Column("source_create_at", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "mattermost_event_state",
        sa.Column("source_update_at", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "mattermost_event_state",
        sa.Column("source_delete_at", sa.BigInteger(), nullable=True),
    )
    op.create_table(
        "mattermost_attachment",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("event_id", sa.BigInteger(), nullable=False),
        sa.Column("mattermost_file_id", sa.String(), nullable=False),
        sa.Column("source_post_id", sa.String(), nullable=False),
        sa.Column("uploader_user_id", sa.String(), nullable=False),
        sa.Column("filename", sa.String(), nullable=False),
        sa.Column("mime_type", sa.String(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("root_post_id", sa.String(), nullable=True),
        sa.Column("create_at", sa.BigInteger(), nullable=True),
        sa.Column("file_store_id", sa.String(), nullable=True),
        sa.Column("user_file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("promoted_seafile_path", sa.String(), nullable=True),
        sa.Column("promoted_seafile_file_id", sa.String(), nullable=True),
        sa.Column("promoted_seafile_revision", sa.String(), nullable=True),
        sa.Column(
            "time_created",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "time_updated",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["event_id"], ["mattermost_event_state.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_file_id"], ["user_file.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "event_id",
            "mattermost_file_id",
            name="uq_mattermost_attachment_event_file",
        ),
    )
    op.create_index(
        "ix_mattermost_attachment_event_id",
        "mattermost_attachment",
        ["event_id"],
    )
    op.create_index(
        "ix_mattermost_attachment_file_id",
        "mattermost_attachment",
        ["mattermost_file_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mattermost_attachment_file_id", table_name="mattermost_attachment"
    )
    op.drop_index(
        "ix_mattermost_attachment_event_id", table_name="mattermost_attachment"
    )
    op.drop_table("mattermost_attachment")
    op.drop_column("mattermost_event_state", "source_delete_at")
    op.drop_column("mattermost_event_state", "source_update_at")
    op.drop_column("mattermost_event_state", "source_create_at")
    op.drop_column("mattermost_event_state", "source_display_name")
    op.drop_column("mattermost_event_state", "source_username")
    op.drop_column("mattermost_event_state", "source_user_id")
    op.drop_column("mattermost_event_state", "root_post_id")

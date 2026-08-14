"""add durable Mattermost event state

Revision ID: c83d1e7a4b6f
Revises: b72c9e4f1a2d
Create Date: 2026-08-14
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c83d1e7a4b6f"
down_revision: str | None = "b72c9e4f1a2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "chat_message",
        sa.Column("external_idempotency_key", sa.String(), nullable=True),
    )
    op.create_index(
        "uq_chat_message_external_idempotency_key",
        "chat_message",
        ["external_idempotency_key"],
        unique=True,
        postgresql_where=sa.text("external_idempotency_key IS NOT NULL"),
    )
    op.create_table(
        "mattermost_event_state",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("instance_id", sa.String(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("dedupe_key", sa.String(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("mapping_id", sa.Integer(), nullable=True),
        sa.Column("source_post_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), server_default="claimed", nullable=False),
        sa.Column("claim_owner", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("mattermost_pending_post_id", sa.String(), nullable=False),
        sa.Column("mattermost_post_id", sa.String(), nullable=True),
        sa.Column("onyx_user_message_id", sa.Integer(), nullable=True),
        sa.Column("onyx_assistant_message_id", sa.Integer(), nullable=True),
        sa.Column("feedback_id", sa.Integer(), nullable=True),
        sa.Column("rendered_message", sa.Text(), nullable=True),
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
            ["mapping_id"],
            ["mattermost_thread_mapping.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["onyx_user_message_id"], ["chat_message.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["onyx_assistant_message_id"],
            ["chat_message.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["feedback_id"], ["chat_feedback.id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "instance_id",
            "channel_id",
            "dedupe_key",
            name="uq_mattermost_event_state_event",
        ),
        sa.UniqueConstraint(
            "mattermost_post_id", name="uq_mattermost_event_state_post_id"
        ),
    )
    op.create_index(
        "ix_mattermost_event_state_claim",
        "mattermost_event_state",
        ["state", "lease_expires_at"],
    )
    op.create_index(
        "ix_mattermost_event_state_mapping_id",
        "mattermost_event_state",
        ["mapping_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mattermost_event_state_mapping_id", table_name="mattermost_event_state"
    )
    op.drop_index(
        "ix_mattermost_event_state_claim", table_name="mattermost_event_state"
    )
    op.drop_table("mattermost_event_state")
    op.drop_index("uq_chat_message_external_idempotency_key", table_name="chat_message")
    op.drop_column("chat_message", "external_idempotency_key")

"""add mattermost thread mapping

Revision ID: a14eb2f1d9c0
Revises: 3350a25df58e
Create Date: 2026-08-14 01:30:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "a14eb2f1d9c0"
down_revision = "3350a25df58e"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mattermost_thread_mapping",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("server_id", sa.String(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("root_id", sa.String(), nullable=False),
        sa.Column("mattermost_user_id", sa.String(), nullable=False),
        sa.Column("persona_id", sa.Integer(), nullable=True),
        sa.Column("chat_session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_message_id", sa.Integer(), nullable=True),
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
            ["chat_session_id"], ["chat_session.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_message_id"], ["chat_message.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["persona_id"], ["persona.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "server_id",
            "channel_id",
            "root_id",
            name="uq_mattermost_thread_mapping_thread",
        ),
        sa.UniqueConstraint(
            "chat_session_id", name="uq_mattermost_thread_mapping_chat_session_id"
        ),
    )
    op.create_index(
        "ix_mattermost_thread_mapping_thread_lookup",
        "mattermost_thread_mapping",
        ["server_id", "channel_id", "root_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mattermost_thread_mapping_thread_lookup",
        table_name="mattermost_thread_mapping",
    )
    op.drop_table("mattermost_thread_mapping")

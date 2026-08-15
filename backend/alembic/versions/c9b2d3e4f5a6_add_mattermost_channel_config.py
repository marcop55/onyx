"""add mattermost channel config

Revision ID: c9b2d3e4f5a6
Revises: f0a1b2c3d4e5
Create Date: 2026-08-15 11:35:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "c9b2d3e4f5a6"
down_revision = "f0a1b2c3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mattermost_channel_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("mattermost_bot_id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=True),
        sa.Column("channel_name", sa.String(), nullable=True),
        sa.Column("persona_id", sa.Integer(), nullable=True),
        sa.Column(
            "channel_config", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["mattermost_bot_id"], ["mattermost_bot.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["persona_id"], ["persona.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "mattermost_bot_id",
            "channel_id",
            name="uq_mattermost_channel_config_bot_channel",
        ),
    )
    op.create_index(
        "ix_mattermost_channel_config_bot_default",
        "mattermost_channel_config",
        ["mattermost_bot_id", "is_default"],
        unique=True,
        postgresql_where=sa.text("is_default IS TRUE"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mattermost_channel_config_bot_default",
        table_name="mattermost_channel_config",
    )
    op.drop_table("mattermost_channel_config")

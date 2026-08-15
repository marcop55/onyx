"""extend mattermost channel config

Revision ID: c9b2d3e4f5a6
Revises: 9f8e7d6c5b4a
Create Date: 2026-08-15 11:35:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "c9b2d3e4f5a6"
down_revision = "9f8e7d6c5b4a"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "mattermost_channel_config",
        "channel_id",
        existing_type=sa.String(),
        nullable=True,
    )
    op.add_column(
        "mattermost_channel_config",
        sa.Column("channel_name", sa.String(), nullable=True),
    )
    op.add_column(
        "mattermost_channel_config",
        sa.Column("persona_id", sa.Integer(), nullable=True),
    )
    op.add_column(
        "mattermost_channel_config",
        sa.Column(
            "channel_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text(
                '\'{"channel_name": null, "respond_tag_only": true, '
                '"response_style": "orka_concise", "disabled": false}\'::jsonb'
            ),
        ),
    )
    op.add_column(
        "mattermost_channel_config",
        sa.Column(
            "is_default", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )
    op.create_foreign_key(
        "fk_mattermost_channel_config_persona_id_persona",
        "mattermost_channel_config",
        "persona",
        ["persona_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_mattermost_channel_config_bot_default",
        "mattermost_channel_config",
        ["mattermost_bot_id", "is_default"],
        unique=True,
        postgresql_where=sa.text("is_default IS TRUE"),
    )
    op.alter_column(
        "mattermost_channel_config",
        "channel_config",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mattermost_channel_config_bot_default",
        table_name="mattermost_channel_config",
    )
    op.drop_constraint(
        "fk_mattermost_channel_config_persona_id_persona",
        "mattermost_channel_config",
        type_="foreignkey",
    )
    op.drop_column("mattermost_channel_config", "is_default")
    op.drop_column("mattermost_channel_config", "channel_config")
    op.drop_column("mattermost_channel_config", "persona_id")
    op.drop_column("mattermost_channel_config", "channel_name")
    op.alter_column(
        "mattermost_channel_config",
        "channel_id",
        existing_type=sa.String(),
        nullable=False,
    )

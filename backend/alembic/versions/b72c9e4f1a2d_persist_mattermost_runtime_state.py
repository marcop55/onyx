"""persist Mattermost adapter runtime state

Revision ID: b72c9e4f1a2d
Revises: a14eb2f1d9c0
Create Date: 2026-08-14 11:42:00.000000

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "b72c9e4f1a2d"
down_revision = "a14eb2f1d9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "mattermost_thread_mapping",
        sa.Column(
            "answer_post_message_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "mattermost_thread_mapping",
        sa.Column(
            "processed_event_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "mattermost_thread_mapping",
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("mattermost_thread_mapping", "is_active")
    op.drop_column("mattermost_thread_mapping", "processed_event_ids")
    op.drop_column("mattermost_thread_mapping", "answer_post_message_ids")

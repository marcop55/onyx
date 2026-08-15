"""add Mattermost attachment placement proposals

Revision ID: d4e5f6a7b8c9
Revises: c9b2d3e4f5a6
Create Date: 2026-08-15 16:00:00.000000
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = "d4e5f6a7b8c9"
down_revision = "c9b2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mattermost_attachment_placement_proposal",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("attachment_id", sa.BigInteger(), nullable=False),
        sa.Column("proposal_identity", sa.String(length=64), nullable=False),
        sa.Column("mattermost_file_id", sa.String(), nullable=False),
        sa.Column("source_post_id", sa.String(), nullable=False),
        sa.Column("uploader_user_id", sa.String(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("root_post_id", sa.String(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("library_id", sa.String(), nullable=False),
        sa.Column("proposed_root", sa.String(), nullable=False),
        sa.Column("proposed_path", sa.String(), nullable=False),
        sa.Column("normalized_filename", sa.String(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "should_remain_temporary",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("hierarchy_root_revision", sa.String(), nullable=False),
        sa.Column(
            "duplicate_conflict_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "audit_evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "rollback_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("ingestion_freshness_proof", sa.String(), nullable=True),
        sa.Column("readback_file_id", sa.String(), nullable=True),
        sa.Column("readback_revision", sa.String(), nullable=True),
        sa.Column("promotion_confirmer_user_id", sa.String(), nullable=True),
        sa.Column("promotion_claimed_at", sa.DateTime(timezone=True), nullable=True),
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
            ["attachment_id"],
            ["mattermost_attachment.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "proposal_identity",
            name="uq_mattermost_attachment_placement_proposal_identity",
        ),
    )
    op.create_index(
        "ix_mattermost_attachment_placement_proposal_attachment_id",
        "mattermost_attachment_placement_proposal",
        ["attachment_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mattermost_attachment_placement_proposal_attachment_id",
        table_name="mattermost_attachment_placement_proposal",
    )
    op.drop_table("mattermost_attachment_placement_proposal")

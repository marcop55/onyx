"""merge Mattermost bot config heads

Revision ID: f0a1b2c3d4e5
Revises: b8a1c2d3e4f5, e13a5d78b9c2
Create Date: 2026-08-15
"""

from collections.abc import Sequence

revision: str = "f0a1b2c3d4e5"
down_revision: str | Sequence[str] | None = ("b8a1c2d3e4f5", "e13a5d78b9c2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

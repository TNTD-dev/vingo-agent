"""Lưu số đo input cho bộ ước lượng token của turn tiếp theo.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE agent.turn ADD COLUMN last_input_tokens INTEGER NULL")
    op.execute("ALTER TABLE agent.turn ADD COLUMN last_input_chars INTEGER NULL")


def downgrade() -> None:
    op.execute("ALTER TABLE agent.turn DROP COLUMN last_input_chars")
    op.execute("ALTER TABLE agent.turn DROP COLUMN last_input_tokens")

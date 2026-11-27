"""Lưu file SVG tạm theo chủ phiên chat.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE agent.chat_upload (
            id uuid PRIMARY KEY DEFAULT uuidv7(),
            owner_id uuid NOT NULL,
            content bytea NOT NULL,
            expires_at timestamptz NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_chat_upload_expires_at ON agent.chat_upload (expires_at)")
    op.execute("CREATE INDEX ix_chat_upload_owner_id ON agent.chat_upload (owner_id)")


def downgrade() -> None:
    op.execute("DROP TABLE agent.chat_upload")

"""Schema agent: 7 bảng theo V2.2 mục 6.1.

Revision ID: 0001
Revises:
DDL viết cứng (không import models) để migration không đổi khi models.py đổi.
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

DDL = """
CREATE TABLE agent.conversation (
	id UUID DEFAULT uuidv7() NOT NULL,
	owner_id UUID NOT NULL,
	role VARCHAR(16) NOT NULL,
	active_turn_id UUID,
	active_until TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	last_active_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_role CHECK (role IN ('BUYER', 'ADMIN'))
);
CREATE INDEX ix_conversation_owner ON agent.conversation (owner_id, last_active_at);
CREATE TABLE agent.resource_ref (
	conversation_id UUID NOT NULL,
	resource_type VARCHAR(32) NOT NULL,
	resource_id UUID NOT NULL,
	first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_resource_ref PRIMARY KEY (conversation_id, resource_type, resource_id),
	FOREIGN KEY(conversation_id) REFERENCES agent.conversation (id) ON DELETE CASCADE
);
CREATE TABLE agent.summary (
	conversation_id UUID NOT NULL,
	covered_through_seq BIGINT NOT NULL,
	content TEXT NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	PRIMARY KEY (conversation_id),
	FOREIGN KEY(conversation_id) REFERENCES agent.conversation (id) ON DELETE CASCADE
);
CREATE TABLE agent.turn (
	id UUID DEFAULT uuidv7() NOT NULL,
	conversation_id UUID NOT NULL,
	command_id UUID NOT NULL,
	command_hash VARCHAR(64) NOT NULL,
	state VARCHAR(32) NOT NULL,
	final_text_and_blocks JSONB,
	model_version VARCHAR(128),
	prompt_version VARCHAR(64),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_turn_command UNIQUE (conversation_id, command_id),
	CONSTRAINT ck_state CHECK (state IN ('RUNNING', 'COMPLETED', 'AWAITING_CONFIRMATION', 'FAILED')),
	FOREIGN KEY(conversation_id) REFERENCES agent.conversation (id) ON DELETE CASCADE
);
CREATE TABLE agent.message (
	conversation_id UUID NOT NULL,
	seq BIGINT NOT NULL,
	turn_id UUID,
	kind VARCHAR(16) NOT NULL,
	content JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	CONSTRAINT pk_message PRIMARY KEY (conversation_id, seq),
	CONSTRAINT ck_kind CHECK (kind IN ('USER', 'ASSISTANT', 'TOOL_CALL', 'TOOL_RESULT', 'SYSTEM_EVENT')),
	FOREIGN KEY(conversation_id) REFERENCES agent.conversation (id) ON DELETE CASCADE,
	FOREIGN KEY(turn_id) REFERENCES agent.turn (id) ON DELETE CASCADE
);
CREATE TABLE agent.pending_action (
	id UUID DEFAULT uuidv7() NOT NULL,
	conversation_id UUID NOT NULL,
	turn_id UUID,
	actor_id UUID NOT NULL,
	role VARCHAR(16) NOT NULL,
	operation VARCHAR(32) NOT NULL,
	state VARCHAR(16) NOT NULL,
	payload JSONB NOT NULL,
	display JSONB NOT NULL,
	version INTEGER DEFAULT 1 NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	confirmed_via VARCHAR(8),
	created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	decided_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT ck_state CHECK (state IN ('PENDING', 'CONFIRMED', 'REJECTED', 'EXPIRED', 'SUPERSEDED')),
	CONSTRAINT ck_confirmed_via CHECK (confirmed_via IS NULL OR confirmed_via IN ('BUTTON', 'TEXT')),
	FOREIGN KEY(conversation_id) REFERENCES agent.conversation (id) ON DELETE CASCADE,
	FOREIGN KEY(turn_id) REFERENCES agent.turn (id) ON DELETE SET NULL
);
CREATE UNIQUE INDEX uq_pending_action_one_pending ON agent.pending_action (conversation_id) WHERE state = 'PENDING';
CREATE TABLE agent.execution (
	action_id UUID NOT NULL,
	idempotency_key UUID NOT NULL,
	state VARCHAR(16) NOT NULL,
	error_code VARCHAR(64),
	result JSONB,
	started_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
	finished_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (action_id),
	CONSTRAINT ck_state CHECK (state IN ('RUNNING', 'SUCCEEDED', 'FAILED', 'UNKNOWN')),
	FOREIGN KEY(action_id) REFERENCES agent.pending_action (id) ON DELETE CASCADE,
	UNIQUE (idempotency_key)
);
"""


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS agent")
    for statement in DDL.split(";"):
        if statement.strip():
            op.execute(statement)


def downgrade() -> None:
    for table in (
        "execution",
        "pending_action",
        "message",
        "turn",
        "summary",
        "resource_ref",
        "conversation",
    ):
        op.execute(f"DROP TABLE IF EXISTS agent.{table}")

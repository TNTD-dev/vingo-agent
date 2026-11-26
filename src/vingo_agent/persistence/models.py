"""7 bảng schema `agent` (V2.2 mục 6.1). Migration 0001 là nguồn của DDL; file này phải khớp."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP, UUID

SCHEMA = "agent"
metadata = MetaData(schema=SCHEMA)

TS = TIMESTAMP(timezone=True)
UUIDV7 = text("uuidv7()")
NOW = text("now()")


def _check_in(column: str, values: tuple[str, ...]) -> CheckConstraint:
    joined = ", ".join(f"'{v}'" for v in values)
    return CheckConstraint(f"{column} IN ({joined})", name=f"ck_{column}")


ROLES = ("BUYER", "ADMIN")
TURN_STATES = ("RUNNING", "COMPLETED", "AWAITING_CONFIRMATION", "FAILED")
MESSAGE_KINDS = ("USER", "ASSISTANT", "TOOL_CALL", "TOOL_RESULT", "SYSTEM_EVENT")
ACTION_STATES = ("PENDING", "CONFIRMED", "REJECTED", "EXPIRED", "SUPERSEDED")
EXECUTION_STATES = ("RUNNING", "SUCCEEDED", "FAILED", "UNKNOWN")

conversation = Table(
    "conversation",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=UUIDV7),
    Column("owner_id", UUID(as_uuid=True), nullable=False),
    Column("role", String(16), nullable=False),
    Column("active_turn_id", UUID(as_uuid=True)),
    Column("active_until", TS),
    Column("created_at", TS, nullable=False, server_default=NOW),
    Column("last_active_at", TS, nullable=False, server_default=NOW),
    _check_in("role", ROLES),
    Index("ix_conversation_owner", "owner_id", "last_active_at"),
)

turn = Table(
    "turn",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=UUIDV7),
    Column(
        "conversation_id",
        UUID(as_uuid=True),
        ForeignKey("conversation.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("command_id", UUID(as_uuid=True), nullable=False),
    Column("command_hash", String(64), nullable=False),
    Column("state", String(32), nullable=False),
    Column("final_text_and_blocks", JSONB),
    Column("model_version", String(128)),
    Column("prompt_version", String(64)),
    Column("created_at", TS, nullable=False, server_default=NOW),
    Column("finished_at", TS),
    UniqueConstraint("conversation_id", "command_id", name="uq_turn_command"),
    _check_in("state", TURN_STATES),
)

message = Table(
    "message",
    metadata,
    Column(
        "conversation_id",
        UUID(as_uuid=True),
        ForeignKey("conversation.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("seq", BigInteger, nullable=False),
    Column("turn_id", UUID(as_uuid=True), ForeignKey("turn.id", ondelete="CASCADE")),
    Column("kind", String(16), nullable=False),
    Column("content", JSONB, nullable=False),
    Column("created_at", TS, nullable=False, server_default=NOW),
    PrimaryKeyConstraint("conversation_id", "seq", name="pk_message"),
    _check_in("kind", MESSAGE_KINDS),
)

summary = Table(
    "summary",
    metadata,
    Column(
        "conversation_id",
        UUID(as_uuid=True),
        ForeignKey("conversation.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("covered_through_seq", BigInteger, nullable=False),
    Column("content", Text, nullable=False),
    Column("updated_at", TS, nullable=False, server_default=NOW),
)

resource_ref = Table(
    "resource_ref",
    metadata,
    Column(
        "conversation_id",
        UUID(as_uuid=True),
        ForeignKey("conversation.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("resource_type", String(32), nullable=False),
    Column("resource_id", UUID(as_uuid=True), nullable=False),
    Column("first_seen_at", TS, nullable=False, server_default=NOW),
    PrimaryKeyConstraint("conversation_id", "resource_type", "resource_id", name="pk_resource_ref"),
)

pending_action = Table(
    "pending_action",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True, server_default=UUIDV7),
    Column(
        "conversation_id",
        UUID(as_uuid=True),
        ForeignKey("conversation.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("turn_id", UUID(as_uuid=True), ForeignKey("turn.id", ondelete="SET NULL")),
    Column("actor_id", UUID(as_uuid=True), nullable=False),
    Column("role", String(16), nullable=False),
    Column("operation", String(32), nullable=False),
    Column("state", String(16), nullable=False),
    Column("payload", JSONB, nullable=False),
    Column("display", JSONB, nullable=False),
    Column("version", Integer, nullable=False, server_default=text("1")),
    Column("expires_at", TS, nullable=False),
    Column("confirmed_via", String(8)),
    Column("created_at", TS, nullable=False, server_default=NOW),
    Column("decided_at", TS),
    _check_in("state", ACTION_STATES),
    CheckConstraint("confirmed_via IS NULL OR confirmed_via IN ('BUTTON', 'TEXT')", name="ck_confirmed_via"),
    # Một đề xuất PENDING mỗi hội thoại.
    Index(
        "uq_pending_action_one_pending",
        "conversation_id",
        unique=True,
        postgresql_where=text("state = 'PENDING'"),
    ),
)

execution = Table(
    "execution",
    metadata,
    Column(
        "action_id",
        UUID(as_uuid=True),
        ForeignKey("pending_action.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("idempotency_key", UUID(as_uuid=True), nullable=False, unique=True),
    Column("state", String(16), nullable=False),
    Column("error_code", String(64)),
    Column("result", JSONB),
    Column("started_at", TS, nullable=False, server_default=NOW),
    Column("finished_at", TS),
    _check_in("state", EXECUTION_STATES),
)

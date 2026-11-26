"""Truy vấn Core; mỗi hàm tự đóng transaction trước khi trả về."""

from datetime import timedelta

from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from vingo_agent.persistence import models as m
from vingo_agent.persistence.db import transaction


async def create_conversation(owner_id, role):
    async with transaction() as conn:
        return dict(
            (await conn.execute(insert(m.conversation).values(owner_id=owner_id, role=role).returning(m.conversation)))
            .mappings()
            .one()
        )


async def get_conversation(conversation_id):
    async with transaction() as conn:
        return (
            (await conn.execute(select(m.conversation).where(m.conversation.c.id == conversation_id)))
            .mappings()
            .one_or_none()
        )


async def create_turn(conversation_id, command_id, command_hash):
    async with transaction() as conn:
        created = (
            await conn.execute(
                pg_insert(m.turn)
                .values(
                    conversation_id=conversation_id, command_id=command_id, command_hash=command_hash, state="RUNNING"
                )
                .on_conflict_do_nothing(index_elements=["conversation_id", "command_id"])
                .returning(m.turn.c.id)
            )
        ).scalar_one_or_none()
        row = (
            (
                await conn.execute(
                    select(m.turn).where(m.turn.c.conversation_id == conversation_id, m.turn.c.command_id == command_id)
                )
            )
            .mappings()
            .one()
        )
        if row["command_hash"] != command_hash:
            raise ValueError("COMMAND_CONFLICT")
        return {**dict(row), "_created": created is not None}


async def transition_turn(turn_id, expected, state, *, final_text_and_blocks=None):
    async with transaction() as conn:
        result = await conn.execute(
            update(m.turn)
            .where(m.turn.c.id == turn_id, m.turn.c.state == expected)
            .values(state=state, final_text_and_blocks=final_text_and_blocks, finished_at=func.now())
        )
        return result.rowcount == 1


async def transition_action(action_id, expected, version, state):
    async with transaction() as conn:
        result = await conn.execute(
            update(m.pending_action)
            .where(
                m.pending_action.c.id == action_id,
                m.pending_action.c.state == expected,
                m.pending_action.c.version == version,
            )
            .values(state=state, version=version + 1, decided_at=func.now())
        )
        return result.rowcount == 1


async def acquire_turn(conversation_id, turn_id, lease_s: float) -> bool:
    async with transaction() as conn:
        result = await conn.execute(
            update(m.conversation)
            .where(
                m.conversation.c.id == conversation_id,
                or_(m.conversation.c.active_turn_id.is_(None), m.conversation.c.active_until < func.now()),
            )
            .values(
                active_turn_id=turn_id, active_until=func.now() + timedelta(seconds=lease_s), last_active_at=func.now()
            )
        )
        return result.rowcount == 1


async def release_turn(conversation_id, turn_id) -> None:
    async with transaction() as conn:
        await conn.execute(
            update(m.conversation)
            .where(m.conversation.c.id == conversation_id, m.conversation.c.active_turn_id == turn_id)
            .values(active_turn_id=None, active_until=None)
        )


async def append_messages(conversation_id, turn_id, messages: list[dict]) -> None:
    async with transaction() as conn:
        # Khóa hội thoại để cấp seq an toàn giữa các writer.
        (
            await conn.execute(
                select(m.conversation.c.id).where(m.conversation.c.id == conversation_id).with_for_update()
            )
        ).one()
        if turn_id is not None:
            (
                await conn.execute(
                    select(m.turn.c.id).where(m.turn.c.id == turn_id, m.turn.c.conversation_id == conversation_id)
                )
            ).one()
        seq = (
            await conn.execute(
                select(func.coalesce(func.max(m.message.c.seq), 0)).where(
                    m.message.c.conversation_id == conversation_id
                )
            )
        ).scalar_one()
        if messages:
            await conn.execute(
                insert(m.message),
                [
                    dict(
                        conversation_id=conversation_id,
                        turn_id=turn_id,
                        seq=seq + i + 1,
                        kind=item["kind"],
                        content=item["content"],
                    )
                    for i, item in enumerate(messages)
                ],
            )
            await conn.execute(
                update(m.conversation).where(m.conversation.c.id == conversation_id).values(last_active_at=func.now())
            )


async def load_context(conversation_id):
    async with transaction() as conn:
        summary = (
            (await conn.execute(select(m.summary).where(m.summary.c.conversation_id == conversation_id)))
            .mappings()
            .one_or_none()
        )
        rows = (
            (
                await conn.execute(
                    select(m.message)
                    .where(
                        m.message.c.conversation_id == conversation_id,
                        m.message.c.seq > (summary["covered_through_seq"] if summary else 0),
                    )
                    .order_by(m.message.c.seq)
                )
            )
            .mappings()
            .all()
        )
        return summary, rows


async def save_summary(conversation_id, covered_through_seq, content):
    async with transaction() as conn:
        stmt = pg_insert(m.summary).values(
            conversation_id=conversation_id, covered_through_seq=covered_through_seq, content=content
        )
        await conn.execute(
            stmt.on_conflict_do_update(
                index_elements=["conversation_id"],
                set_=dict(covered_through_seq=covered_through_seq, content=content, updated_at=func.now()),
                where=m.summary.c.covered_through_seq < covered_through_seq,
            )
        )


async def has_ref(conversation_id, resource_type, resource_id):
    async with transaction() as conn:
        return (
            await conn.execute(
                select(m.resource_ref.c.resource_id).where(
                    m.resource_ref.c.conversation_id == conversation_id,
                    m.resource_ref.c.resource_type == resource_type,
                    m.resource_ref.c.resource_id == resource_id,
                )
            )
        ).first() is not None


async def add_refs(conversation_id, resource_type, ids):
    if not ids:
        return
    async with transaction() as conn:
        await conn.execute(
            pg_insert(m.resource_ref)
            .values(
                [
                    dict(conversation_id=conversation_id, resource_type=resource_type, resource_id=value)
                    for value in set(ids)
                ]
            )
            .on_conflict_do_nothing()
        )


async def get_turn(turn_id):
    async with transaction() as conn:
        return (await conn.execute(select(m.turn).where(m.turn.c.id == turn_id))).mappings().one_or_none()


async def get_messages(conversation_id):
    async with transaction() as conn:
        return (
            (
                await conn.execute(
                    select(m.message).where(m.message.c.conversation_id == conversation_id).order_by(m.message.c.seq)
                )
            )
            .mappings()
            .all()
        )


async def get_pending_action(conversation_id):
    async with transaction() as conn:
        row = (
            (
                await conn.execute(
                    select(m.pending_action).where(
                        m.pending_action.c.conversation_id == conversation_id,
                        m.pending_action.c.state == "PENDING",
                        m.pending_action.c.expires_at > func.now(),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if not row:
            return None
        return {"action_id": row["id"], **{key: row[key] for key in ("version", "operation", "expires_at", "display")}}


async def set_model_version(turn_id, model_version):
    async with transaction() as conn:
        await conn.execute(update(m.turn).where(m.turn.c.id == turn_id).values(model_version=model_version))


async def discard_unstarted_turn(turn_id):
    async with transaction() as conn:
        await conn.execute(delete(m.turn).where(m.turn.c.id == turn_id, m.turn.c.state == "RUNNING"))


async def set_input_usage(turn_id, input_tokens, input_chars):
    async with transaction() as conn:
        await conn.execute(
            update(m.turn)
            .where(m.turn.c.id == turn_id)
            .values(last_input_tokens=input_tokens, last_input_chars=input_chars)
        )


async def get_last_input_usage(conversation_id):
    """Chỉ lấy số đo hợp lệ của turn thành công gần nhất trong chính hội thoại."""
    async with transaction() as conn:
        return (
            (
                await conn.execute(
                    select(m.turn.c.last_input_tokens, m.turn.c.last_input_chars)
                    .where(
                        m.turn.c.conversation_id == conversation_id,
                        m.turn.c.state.in_(("COMPLETED", "AWAITING_CONFIRMATION")),
                        m.turn.c.last_input_tokens > 0,
                        m.turn.c.last_input_chars > 0,
                    )
                    .order_by(m.turn.c.finished_at.desc(), m.turn.c.id.desc())
                    .limit(1)
                )
            )
            .mappings()
            .one_or_none()
        )

"""Lưu SVG ngắn hạn, chỉ chủ upload được đọc hoặc gắn vào hội thoại."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, insert, select, text

from vingo_agent.persistence.db import transaction
from vingo_agent.persistence.models import chat_upload

UPLOAD_TTL = timedelta(hours=1)
MAX_UPLOADS_PER_OWNER = 8


async def save_upload(owner_id, content):
    now = datetime.now(UTC)
    async with transaction() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:owner, 0))"), {"owner": str(owner_id)})
        await conn.execute(delete(chat_upload).where(chat_upload.c.expires_at <= now))
        count = await conn.scalar(
            select(func.count()).select_from(chat_upload).where(chat_upload.c.owner_id == owner_id)
        )
        if count >= MAX_UPLOADS_PER_OWNER:
            return None
        return await conn.scalar(
            insert(chat_upload)
            .values(owner_id=owner_id, content=content, expires_at=now + UPLOAD_TTL)
            .returning(chat_upload.c.id)
        )


async def get_uploads(owner_id, ids, *, with_content=False):
    columns = [chat_upload.c.id, chat_upload.c.content] if with_content else [chat_upload.c.id]
    async with transaction() as conn:
        rows = await conn.execute(
            select(*columns).where(
                chat_upload.c.owner_id == owner_id, chat_upload.c.id.in_(ids), chat_upload.c.expires_at > func.now()
            )
        )
        return {row.id: row.content if with_content else None for row in rows}

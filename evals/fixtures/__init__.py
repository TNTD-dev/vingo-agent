"""Seed/reset SQL theo Flyway v2; chỉ DB eval riêng, không gọi admin production."""

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

import asyncpg

from evals.case_io import VN, time_variables

# Hash bcrypt giả cho A1, không có mật khẩu tương ứng đã biết. Eval dùng login_session.
_ADMIN_PASSWORD_HASH = "$2a$10$og2ENJU/dRNsxJBSFZUbM.mgCYBloRpi8zb.kr6ndTlfF2BqHjWZ2"

ALIASES = {
    key: str(uuid5(NAMESPACE_URL, "vingo-eval:" + key))
    for key in (
        "B1",
        "B2",
        "A1",
        "venue",
        "venue_map",
        "layout",
        "evt_01",
        "evt_draft",
        "st_01",
        "st_metric",
        "zone_ga",
        "zone_vip",
        "s1",
        "s2",
        "tier_ga",
        "tier_vip",
        "price_ga",
        "price_vip",
        "ord_b1",
        "ord_b2",
        "paid_b1",
        "paid_b2",
        "tick_b1",
        "tick_b2",
    )
}
BUSINESS_TABLES = (
    "ticket_order",
    "seat_inventory",
    "standing_inventory",
    "payment",
    "event",
    "showtime",
    "ticket",
    "notification_outbox",
    "venue",
    "ticket_type",
    "ticket_type_price",
    "event_zone",
)


def check_reset(backend_url: str, *database_urls: str, environ=None) -> None:
    env = os.environ if environ is None else environ
    if env.get("EVAL_ALLOW_RESET") != "1":
        raise ValueError("RESET_NOT_AUTHORIZED: cần EVAL_ALLOW_RESET=1")
    for url in (backend_url, *database_urls):
        parsed = urlsplit(url)
        if parsed.query or parsed.fragment:
            raise ValueError("RESET_UNSAFE_URL")
        if not parsed.hostname or parsed.scheme not in {"http", "https", "postgresql", "postgresql+asyncpg"}:
            raise ValueError("RESET_UNSAFE_URL")
        # Dấu hiệu nằm ở host hoặc tên DB, không nằm ở password/query/user.
        target = parsed.hostname + (parsed.path if parsed.scheme.startswith("postgresql") else "")
        if re.search(r"prod(?:uction)?", target, re.I):
            raise ValueError("RESET_PRODUCTION_FORBIDDEN")
        if not (
            parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            or re.search(r"(?:^|[-_./])(?:test|eval)(?:$|[-_./0-9])", target, re.I)
        ):
            raise ValueError("RESET_UNSAFE_TARGET")
    # DB riêng giúp không xóa dữ liệu dev trên localhost/vingo vô tình.
    for url in database_urls:
        if not re.search(r"(?:^|[_-])(?:eval|test)(?:$|[_-]|\d)", urlsplit(url).path.lstrip("/"), re.I):
            raise ValueError("RESET_REQUIRES_DEDICATED_DATABASE")


def dsn(url):
    return url.replace("postgresql+asyncpg://", "postgresql://", 1)


async def connect(url):
    return await asyncpg.connect(dsn(url))


async def reset(backend_url, backend_db, agent_db, t0: datetime, variant="seed") -> dict:
    check_reset(backend_url, backend_db, agent_db)
    variables = time_variables(t0)
    ids = ALIASES
    sessions = {actor: secrets.token_urlsafe(32) for actor in ("B1", "B2", "A1")}
    conn = await connect(backend_db)
    try:
        async with conn.transaction():
            # Không CASCADE sang schema ngoài public/agent; danh sách lấy từ catalog, quote identifier.
            tables = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                "AND tablename NOT IN ('flyway_schema_history') ORDER BY tablename"
            )
            if not tables:
                raise ValueError("BACKEND_MIGRATIONS_REQUIRED")
            names = ",".join('public."' + row["tablename"].replace('"', '""') + '"' for row in tables)
            await conn.execute("TRUNCATE " + names)
            for actor in sessions:
                await conn.execute(
                    "INSERT INTO user_account(id,email,full_name,google_sub,role,email_verified_at,password_hash) "
                    "VALUES($1,$2,$3,$4,$5,now(),$6)",
                    ids[actor],
                    actor.lower() + "@example.invalid",
                    actor,
                    "eval-" + actor,
                    "ADMIN" if actor == "A1" else "BUYER",
                    _ADMIN_PASSWORD_HASH if actor == "A1" else None,
                )
                await conn.execute(
                    "INSERT INTO login_session(user_id,token_hash,expires_at) VALUES($1,$2,$3)",
                    ids[actor],
                    hashlib.sha256(sessions[actor].encode()).hexdigest(),
                    t0 + timedelta(days=2),
                )
            await conn.execute(
                "INSERT INTO venue(id,name,address,city) VALUES($1,'Eval Venue','Eval','HN')", ids["venue"]
            )
            await conn.execute(
                "INSERT INTO venue_map(id,venue_id,version,svg) VALUES($1,$2,1,'<svg/>')",
                ids["venue_map"],
                ids["venue"],
            )
            await conn.execute(
                "INSERT INTO seat_map(id,venue_id,venue_map_id,version,name,layout_type,svg) "
                "VALUES($1,$2,$3,1,'Eval Layout','CONCERT','<svg/>')",
                ids["layout"],
                ids["venue"],
                ids["venue_map"],
            )
            for alias, code, kind, capacity in [("zone_ga", "GA", "STANDING", 12), ("zone_vip", "VIP", "SEATED", None)]:
                await conn.execute(
                    "INSERT INTO zone(id,seat_map_id,code,name,kind,capacity) VALUES($1,$2,$3,$3,$4,$5)",
                    ids[alias],
                    ids["layout"],
                    code,
                    kind,
                    capacity,
                )
            for index, alias in enumerate(("s1", "s2"), 1):
                await conn.execute(
                    "INSERT INTO seat(id,seat_map_id,zone_id,row_label,number) VALUES($1,$2,$3,'R1',$4)",
                    ids[alias],
                    ids["layout"],
                    ids["zone_vip"],
                    index,
                )
            for alias, title, status in [("evt_01", "Demo Concert", "PUBLISHED"), ("evt_draft", "Demo Draft", "DRAFT")]:
                await conn.execute(
                    "INSERT INTO event(id,title,category,seat_map_id,status) VALUES($1,$2,'CONCERT',$3,$4)",
                    ids[alias],
                    title,
                    ids["layout"],
                    status,
                )
            for tier, price_id, name, price in [
                ("tier_ga", "price_ga", "GA", 250000),
                ("tier_vip", "price_vip", "VIP", 500000),
            ]:
                await conn.execute(
                    "INSERT INTO ticket_type(id,event_id,name,color) VALUES($1,$2,$3,'#445566')",
                    ids[tier],
                    ids["evt_01"],
                    name,
                )
                await conn.execute(
                    "INSERT INTO ticket_type_price(id,ticket_type_id,price,effective_from,created_by) "
                    "VALUES($1,$2,$3,$4,$5)",
                    ids[price_id],
                    ids[tier],
                    price,
                    t0 - timedelta(days=60),
                    ids["A1"],
                )
            for zone, tier in [("zone_ga", "tier_ga"), ("zone_vip", "tier_vip")]:
                await conn.execute(
                    "INSERT INTO event_zone(event_id,zone_id,seat_map_id,ticket_type_id) VALUES($1,$2,$3,$4)",
                    ids["evt_01"],
                    ids[zone],
                    ids["layout"],
                    ids[tier],
                )
            starts = datetime.fromisoformat(variables["st_01_starts_at"])
            metric_time = t0.astimezone(VN).replace(day=1, hour=0, minute=1, second=0, microsecond=0)
            metric_starts = metric_time.replace(hour=19, minute=0) + timedelta(days=(2 - metric_time.weekday()) % 7)
            for alias, start in [("st_01", starts), ("st_metric", metric_starts)]:
                await conn.execute(
                    "INSERT INTO showtime(id,event_id,seat_map_id,starts_at,ends_at,sale_opens_at,sale_closes_at) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7)",
                    ids[alias],
                    ids["evt_01"],
                    ids["layout"],
                    start,
                    start + timedelta(hours=3),
                    datetime.fromisoformat(variables["sale_opens_at"])
                    if alias == "st_01"
                    else metric_time - timedelta(days=60),
                    datetime.fromisoformat(variables["sale_closes_at"])
                    if alias == "st_01"
                    else start - timedelta(minutes=1),
                )
                await conn.execute(
                    "INSERT INTO standing_inventory(showtime_id,zone_id,seat_map_id,capacity,sold,held) "
                    "VALUES($1,$2,$3,$6,$4,$5)",
                    ids[alias],
                    ids["zone_ga"],
                    ids["layout"],
                    20 if alias == "st_metric" else 0,
                    0 if alias == "st_metric" else 2,
                    20 if alias == "st_metric" else 12,
                )
            await conn.execute(
                "INSERT INTO seat_inventory(showtime_id,seat_id,zone_id,seat_map_id) "
                "SELECT $1,id,zone_id,seat_map_id FROM seat",
                ids["st_01"],
            )
            for actor, order, paid, qty in [
                ("B1", "ord_b1", False, 1),
                ("B2", "ord_b2", False, 1),
                ("B1", "paid_b1", True, 10),
                ("B2", "paid_b2", True, 10),
            ]:
                showtime = ids["st_metric" if paid else "st_01"]
                await conn.execute(
                    "INSERT INTO ticket_order(id,code,user_id,showtime_id,status,total_amount,expires_at,"
                    "buyer_name,buyer_email,buyer_phone,paid_at,created_at) "
                    "VALUES($1,$2,$3,$4,$5,$6,$7,'Eval',$8,'+84900000001',$9,$10)",
                    ids[order],
                    {"ord_b1": "EV0001", "ord_b2": "EV0002", "paid_b1": "EV0003", "paid_b2": "EV0004"}[order],
                    ids[actor],
                    showtime,
                    "PAID" if paid else "PENDING",
                    qty * 250000,
                    t0 + timedelta(days=1),
                    actor.lower() + "@example.invalid",
                    metric_time if paid else None,
                    metric_time if paid else t0,
                )
                await conn.execute(
                    "INSERT INTO order_standing(order_id,showtime_id,zone_id,quantity,unit_price,"
                    "ticket_type_id,ticket_type_price_id) VALUES($1,$2,$3,$4,250000,$5,$6)",
                    ids[order],
                    showtime,
                    ids["zone_ga"],
                    qty,
                    ids["tier_ga"],
                    ids["price_ga"],
                )
                if paid:
                    for i in range(qty):
                        tid = (
                            ids["tick_b1" if actor == "B1" else "tick_b2"]
                            if i == 0
                            else str(uuid5(NAMESPACE_URL, order + str(i)))
                        )
                        await conn.execute(
                            "INSERT INTO ticket(id,order_id,showtime_id,zone_id,seq,code) VALUES($1,$2,$3,$4,$5,$6)",
                            tid,
                            ids[order],
                            showtime,
                            ids["zone_ga"],
                            i + 1,
                            order + str(i),
                        )
                        if actor == "B1" and 1 <= i <= 8:
                            await conn.execute(
                                "UPDATE ticket SET status='USED',used_at=$1 WHERE id=$2", metric_time, tid
                            )
                            await conn.execute(
                                "INSERT INTO ticket_scan(ticket_id,showtime_id,scanned_by,result,scanned_at) "
                                "VALUES($1,$2,$3,'OK',$4)",
                                tid,
                                showtime,
                                ids["A1"],
                                metric_time,
                            )
            await apply_variant(conn, variant, t0)
    finally:
        await conn.close()
    agent = await connect(agent_db)
    try:
        await agent.execute("TRUNCATE agent.conversation CASCADE")
    finally:
        await agent.close()
    return {"aliases": dict(ids), "sessions": sessions, "time": variables}


async def apply_variant(conn, variant, t0):
    if variant == "seed":
        return
    variants = variant if isinstance(variant, dict) else {variant: True}
    for key, value in variants.items():
        if key == "no_pending":
            await conn.execute("UPDATE ticket_order SET status='CANCELLED' WHERE status='PENDING'")
            await conn.execute("UPDATE standing_inventory SET held=0 WHERE showtime_id=$1", ALIASES["st_01"])
        elif key == "stock":
            await conn.execute(
                "UPDATE standing_inventory SET capacity=sold+held+$1 WHERE showtime_id=$2", int(value), ALIASES["st_01"]
            )
        elif key == "seat_status":
            if value not in {"AVAILABLE", "BLOCKED", "SOLD"}:
                raise ValueError("UNSUPPORTED_SEAT_VARIANT")
            if value == "SOLD":
                order = str(uuid5(NAMESPACE_URL, "eval-sold-order"))
                await conn.execute(
                    "INSERT INTO ticket_order(id,code,user_id,showtime_id,status,total_amount,expires_at,"
                    "buyer_name,buyer_email,buyer_phone,paid_at) "
                    "VALUES($1,'SOLD01',$2,$3,'PAID',1000000,$4,'Eval','eval@example.invalid','+84900000002',$4)",
                    order,
                    ALIASES["B2"],
                    ALIASES["st_01"],
                    t0,
                )
                await conn.execute("UPDATE seat_inventory SET status='SOLD',order_id=$1", order)
            else:
                await conn.execute("UPDATE seat_inventory SET status=$1", value)
        elif key == "order_status":
            if value not in {"EXPIRED", "CANCELLED", "PAID"}:
                raise ValueError("UNSUPPORTED_ORDER_VARIANT")
            await conn.execute(
                "UPDATE ticket_order SET status=$1, paid_at=$2 WHERE id=$3",
                value,
                t0 if value == "PAID" else None,
                ALIASES["ord_b1"],
            )
        elif key == "payment_open":
            await conn.execute(
                "INSERT INTO payment(order_id,txn_ref,payment_url) VALUES($1,'eval-open',"
                "'https://sandbox.vnpayment.vn/eval')",
                ALIASES["ord_b1"],
            )
        elif key in {"duplicate_event", "empty_event", "many_events"}:
            count = int(value) if key == "many_events" else 1
            for i in range(count):
                eid = str(uuid5(NAMESPACE_URL, key + str(i)))
                title = (
                    "Demo Concert"
                    if key == "duplicate_event"
                    else "Empty Concert"
                    if key == "empty_event"
                    else f"Eval Page {i:02}"
                )
                await conn.execute(
                    "INSERT INTO event(id,title,category,seat_map_id,status) VALUES($1,$2,'CONCERT',$3,'PUBLISHED')",
                    eid,
                    title,
                    ALIASES["layout"],
                )
                if key != "empty_event":
                    starts = datetime.fromisoformat(time_variables(t0)["st_01_starts_at"])
                    await conn.execute(
                        "INSERT INTO showtime(event_id,seat_map_id,starts_at,ends_at,sale_opens_at,sale_closes_at) "
                        "VALUES($1,$2,$3,$4,$5,$6)",
                        eid,
                        ALIASES["layout"],
                        starts,
                        starts + timedelta(hours=3),
                        t0 - timedelta(days=1),
                        starts - timedelta(hours=1),
                    )
        elif key == "empty_tickets":
            await conn.execute("DELETE FROM ticket_scan")
            await conn.execute("DELETE FROM ticket")
        elif key == "empty_orders":
            await conn.execute("DELETE FROM ticket_scan")
            await conn.execute("DELETE FROM ticket")
            await conn.execute("DELETE FROM order_standing")
            await conn.execute("DELETE FROM order_seat")
            await conn.execute("DELETE FROM order_status_history")
            await conn.execute("DELETE FROM ticket_order")
            await conn.execute("UPDATE standing_inventory SET held=0,sold=0")
        elif key == "metric_zero":
            await conn.execute("UPDATE ticket_order SET paid_at=$1 WHERE status='PAID'", t0 - timedelta(days=60))
        elif key == "description":
            await conn.execute("UPDATE event SET description=$1 WHERE id=$2", str(value), ALIASES["evt_01"])
        else:
            raise ValueError("UNKNOWN_FIXTURE_VARIANT:" + key)


async def snapshot(database_url):
    conn = await connect(database_url)
    try:
        result = {}
        for table in BUSINESS_TABLES:
            rows = await conn.fetch(f'SELECT to_jsonb(t)::text AS row FROM public."{table}" t')
            result[table] = sorted(row["row"] for row in rows)
        return result
    finally:
        await conn.close()


def delta(before, after):
    changed = {
        table: len(set(after[table]) - set(before[table])) + len(set(before[table]) - set(after[table]))
        for table in BUSINESS_TABLES
    }
    # Một dòng đổi tính một hiệu ứng; không tính lịch sử chat hay đề xuất.
    effects = sum(
        max(len(set(after[t]) - set(before[t])), len(set(before[t]) - set(after[t]))) for t in BUSINESS_TABLES
    )

    def held(snap):
        return sum(json.loads(r)["held"] for r in snap["standing_inventory"])

    return effects, {
        "new_orders": len(after["ticket_order"]) - len(before["ticket_order"]),
        "new_payments": len(after["payment"]) - len(before["payment"]),
        "held": held(after) - held(before),
        "changed": changed,
    }

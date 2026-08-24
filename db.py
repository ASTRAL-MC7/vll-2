import os
import asyncio
import logging

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

logger = logging.getLogger("db")

DATABASE_URL = os.environ["DATABASE_URL"]

# Small connection pool - plenty for a Telegram bot's traffic pattern.
_pool = ConnectionPool(conninfo=DATABASE_URL, min_size=1, max_size=10, open=True)


def _execute(query, params=None, fetch=None):
    """fetch: None | 'one' | 'all'"""
    with _pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params or ())
            result = None
            if fetch == "one":
                result = cur.fetchone()
            elif fetch == "all":
                result = cur.fetchall()
            conn.commit()
            return result


async def execute(query, params=None, fetch=None):
    return await asyncio.to_thread(_execute, query, params, fetch)


DEFAULT_REFMESSAGE = (
    "Sizni VL community ga taklif qilamiz! Vcoinlar ishlang va ularni "
    "stars & gift & telegram premiumga aylantiring! Hoziroq boshlang:"
)


def _init_db_sync():
    with _pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    vcoin INTEGER NOT NULL DEFAULT 0,
                    referrer_id BIGINT,
                    verified BOOLEAN NOT NULL DEFAULT FALSE,
                    referral_credited BOOLEAN NOT NULL DEFAULT FALSE,
                    joined_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_referrer ON users(referrer_id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_users_vcoin ON users(vcoin DESC);")

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS channels (
                    id SERIAL PRIMARY KEY,
                    chat_id TEXT NOT NULL UNIQUE,
                    invite_link TEXT,
                    title TEXT
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS admins (
                    user_id BIGINT PRIMARY KEY,
                    added_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                """
            )
            conn.commit()

            # Seed the two default mandatory channels, only if the table is empty.
            cur.execute("SELECT COUNT(*) FROM channels;")
            count = cur.fetchone()[0]
            if count == 0:
                cur.execute(
                    """
                    INSERT INTO channels (chat_id, invite_link, title) VALUES
                    (%s, %s, %s),
                    (%s, %s, %s)
                    """,
                    (
                        "@vllprem", "https://t.me/vllprem", "VL Prem",
                        "-1003975242815", "https://t.me/+5Ky78P3W36wwMTgy", "VL Kanal 2",
                    ),
                )
                conn.commit()

            # Seed default referral message.
            cur.execute("SELECT 1 FROM settings WHERE key='refmessage';")
            if not cur.fetchone():
                cur.execute(
                    "INSERT INTO settings (key, value) VALUES ('refmessage', %s);",
                    (DEFAULT_REFMESSAGE,),
                )
                conn.commit()


async def init_db():
    await asyncio.to_thread(_init_db_sync)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

async def get_user(user_id: int):
    return await execute("SELECT * FROM users WHERE user_id=%s;", (user_id,), fetch="one")


async def create_user_if_not_exists(user_id: int, username, full_name):
    await execute(
        """
        INSERT INTO users (user_id, username, full_name)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET username=EXCLUDED.username, full_name=EXCLUDED.full_name;
        """,
        (user_id, username, full_name),
    )


async def set_referrer_if_absent(user_id: int, referrer_id: int):
    await execute(
        """
        UPDATE users SET referrer_id=%s
        WHERE user_id=%s AND referrer_id IS NULL AND %s <> user_id;
        """,
        (referrer_id, user_id, referrer_id),
    )


async def mark_verified_and_bonus(user_id: int):
    """Marks the user as verified and gives the +1 Vcoin joining bonus.
    Returns the updated row, or None if the user was already verified before."""
    return await execute(
        """
        UPDATE users SET verified=TRUE, vcoin = vcoin + 1
        WHERE user_id=%s AND verified=FALSE
        RETURNING vcoin, referrer_id, referral_credited;
        """,
        (user_id,),
        fetch="one",
    )


async def credit_referrer(referrer_id: int, child_user_id: int):
    row = await execute(
        "UPDATE users SET vcoin = vcoin + 1 WHERE user_id=%s RETURNING vcoin;",
        (referrer_id,),
        fetch="one",
    )
    if row:
        await execute("UPDATE users SET referral_credited=TRUE WHERE user_id=%s;", (child_user_id,))
        return row["vcoin"]
    return None


async def add_vcoin(user_id: int, amount: int):
    return await execute(
        "UPDATE users SET vcoin = vcoin + %s WHERE user_id=%s RETURNING vcoin;",
        (amount, user_id),
        fetch="one",
    )


async def minus_vcoin(user_id: int, amount: int):
    return await execute(
        "UPDATE users SET vcoin = GREATEST(vcoin - %s, 0) WHERE user_id=%s RETURNING vcoin;",
        (amount, user_id),
        fetch="one",
    )


async def count_users():
    row = await execute("SELECT COUNT(*) AS c FROM users;", fetch="one")
    return row["c"]


async def top_users(limit: int = 10):
    return await execute(
        "SELECT user_id, username, vcoin FROM users ORDER BY vcoin DESC LIMIT %s;",
        (limit,),
        fetch="all",
    )


async def get_all_user_ids():
    rows = await execute("SELECT user_id FROM users;", fetch="all")
    return [r["user_id"] for r in rows]


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

async def get_channels():
    return await execute("SELECT * FROM channels ORDER BY id;", fetch="all")


async def add_channel(chat_id: str, invite_link: str, title: str):
    await execute(
        """
        INSERT INTO channels (chat_id, invite_link, title) VALUES (%s, %s, %s)
        ON CONFLICT (chat_id) DO UPDATE SET invite_link=EXCLUDED.invite_link, title=EXCLUDED.title;
        """,
        (chat_id, invite_link, title),
    )


async def remove_channel(chat_id: str):
    row = await execute("DELETE FROM channels WHERE chat_id=%s RETURNING id;", (chat_id,), fetch="one")
    return row is not None


# ---------------------------------------------------------------------------
# Admins
# ---------------------------------------------------------------------------

async def is_admin(user_id: int, main_admin_id: int):
    if user_id == main_admin_id:
        return True
    row = await execute("SELECT 1 FROM admins WHERE user_id=%s;", (user_id,), fetch="one")
    return row is not None


async def add_admin(user_id: int):
    await execute("INSERT INTO admins (user_id) VALUES (%s) ON CONFLICT DO NOTHING;", (user_id,))


async def remove_admin(user_id: int):
    row = await execute("DELETE FROM admins WHERE user_id=%s RETURNING user_id;", (user_id,), fetch="one")
    return row is not None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

async def get_setting(key: str, default=None):
    row = await execute("SELECT value FROM settings WHERE key=%s;", (key,), fetch="one")
    return row["value"] if row else default


async def set_setting(key: str, value: str):
    await execute(
        "INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value;",
        (key, value),
    )

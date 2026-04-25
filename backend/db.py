"""Async SQLite database layer for payment tracking."""

import asyncio

import aiosqlite

from backend.config import DATA_DIR, DB_PATH

_db: aiosqlite.Connection | None = None
_db_lock = asyncio.Lock()


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is not None:
        return _db
    async with _db_lock:
        # Double-check after acquiring the lock
        if _db is not None:
            return _db
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(DB_PATH)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await _init_schema(conn)
        _db = conn
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


async def _init_schema(db: aiosqlite.Connection) -> None:
    await db.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
            vendor        TEXT    NOT NULL,
            url           TEXT    NOT NULL,
            amount_sats   INTEGER NOT NULL,
            payment_hash  TEXT    NOT NULL UNIQUE,
            status        TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'completed', 'failed', 'reserved')),
            description   TEXT    NOT NULL DEFAULT ''
        )
    """)
    await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    await db.commit()


async def get_setting(key: str, default: str = "") -> str:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT value FROM settings WHERE key = ?", (key,)
    )
    return rows[0][0] if rows else default


async def set_setting(key: str, value: str) -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    await db.commit()


async def record_transaction(
    *,
    vendor: str,
    url: str,
    amount_sats: int,
    payment_hash: str,
    status: str = "pending",
    description: str = "",
) -> int:
    db = await get_db()
    cursor = await db.execute(
        """
        INSERT INTO transactions (vendor, url, amount_sats, payment_hash, status, description)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (vendor, url, amount_sats, payment_hash, status, description),
    )
    await db.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def update_transaction_status(payment_hash: str, status: str) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE transactions SET status = ? WHERE payment_hash = ?",
        (status, payment_hash),
    )
    await db.commit()


async def payment_hash_exists(payment_hash: str) -> bool:
    """Check if a payment hash has already been attempted (dedup guard)."""
    db = await get_db()
    row = await db.execute_fetchall(
        "SELECT 1 FROM transactions WHERE payment_hash = ?",
        (payment_hash,),
    )
    return len(row) > 0


async def sum_spending_today() -> int:
    """Sum sats spent today (pending + completed + reserved — not failed)."""
    db = await get_db()
    rows = await db.execute_fetchall(
        """
        SELECT COALESCE(SUM(amount_sats), 0) AS total
        FROM transactions
        WHERE date(created_at) = date('now')
          AND status IN ('pending', 'completed', 'reserved')
        """,
    )
    return rows[0][0]


async def check_budget_and_reserve(
    *,
    daily_limit: int,
    vendor: str,
    url: str,
    amount_sats: int,
    payment_hash: str,
    description: str = "",
) -> int:
    """Atomically check the daily budget and reserve the payment.

    Uses BEGIN IMMEDIATE to prevent concurrent requests from both passing
    the budget check in the gap between SELECT and INSERT (TOCTOU).

    Returns remaining budget after this reservation.
    Raises ValueError if the payment would exceed the daily limit.
    Raises aiosqlite.IntegrityError if payment_hash already exists (dedup).
    """
    db = await get_db()
    # BEGIN IMMEDIATE acquires a write lock before executing, preventing
    # another connection from interleaving a write between our SELECT and INSERT.
    await db.execute("BEGIN IMMEDIATE")
    try:
        rows = await db.execute_fetchall(
            """
            SELECT COALESCE(SUM(amount_sats), 0) AS total
            FROM transactions
            WHERE date(created_at) = date('now')
              AND status IN ('pending', 'completed', 'reserved')
            """,
        )
        spent = rows[0][0]

        if spent + amount_sats > daily_limit:
            await db.execute("ROLLBACK")
            remaining = max(0, daily_limit - spent)
            raise ValueError(
                f"Budget exceeded: requested {amount_sats} sats, "
                f"already spent {spent}/{daily_limit} sats today "
                f"({remaining} sats remaining)"
            )

        await db.execute(
            """
            INSERT INTO transactions (vendor, url, amount_sats, payment_hash, status, description)
            VALUES (?, ?, ?, ?, 'reserved', ?)
            """,
            (vendor, url, amount_sats, payment_hash, description),
        )
        await db.execute("COMMIT")
    except Exception:
        # Ensure we don't leave an open transaction on unexpected errors
        try:
            await db.execute("ROLLBACK")
        except Exception:
            pass
        raise

    return daily_limit - spent - amount_sats


async def get_transactions(limit: int = 50) -> list[dict]:
    db = await get_db()
    rows = await db.execute_fetchall(
        "SELECT * FROM transactions ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    return [dict(r) for r in rows]

"""SQLite persistence engine for Flaschenpost shopping lists and configurations."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .const import DEFAULT_ITEM_WEIGHT, DEFAULT_SHOPPING_LIST_NAME, UNIT_TYPE_UNIT
from .shopping_list import ShoppingListConfig, ShoppingListItem

_LOGGER = logging.getLogger(__name__)

SCHEMA_VERSION = 1


class FlaschenpostSqliteStorage:
    """Non-blocking SQLite storage manager for Flaschenpost shopping lists."""

    def __init__(
        self,
        db_path: str | Path,
        hass: Any | None = None,
    ) -> None:
        """Initialize SQLite storage with path and optional Home Assistant instance."""
        self._db_path = str(db_path)
        self._hass = hass
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    @property
    def db_path(self) -> str:
        """Return path to SQLite database file."""
        return self._db_path

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            # If parent directory doesn't exist, create it (except for in-memory DB)
            if self._db_path != ":memory:":
                Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)

            self._conn = sqlite3.connect(
                self._db_path,
                check_same_thread=False,
                timeout=30.0,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON;")
            if self._db_path != ":memory:":
                with contextlib.suppress(Exception):
                    self._conn.execute("PRAGMA journal_mode = WAL;")
        return self._conn

    async def _async_run(self, func: Callable[..., Any], *args: Any) -> Any:
        """Run blocking SQLite operation safely inside the executor pool."""
        def _wrapper() -> Any:
            with self._lock:
                return func(*args)

        if self._hass is not None and hasattr(self._hass, "async_add_executor_job"):
            return await self._hass.async_add_executor_job(_wrapper)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _wrapper)

    def close(self) -> None:
        """Close SQLite database connection."""
        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(Exception):
                    self._conn.close()
                self._conn = None

    async def async_init(self) -> None:
        """Initialize SQLite schema (tables, indexes, default lists)."""
        def _init() -> None:
            conn = self._get_connection()
            with conn:
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS lists (
                        name TEXT PRIMARY KEY NOT NULL,
                        random_enabled INTEGER NOT NULL DEFAULT 0,
                        random_count INTEGER NOT NULL DEFAULT 1,
                        consume_on_order INTEGER NOT NULL DEFAULT 0,
                        consume_action TEXT NOT NULL DEFAULT 'complete',
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                    );"""
                )
                cursor = conn.cursor()
                cursor.execute("PRAGMA table_info(lists);")
                list_cols = {row["name"] for row in cursor.fetchall()}
                if "consume_on_order" not in list_cols:
                    conn.execute("ALTER TABLE lists ADD COLUMN consume_on_order INTEGER NOT NULL DEFAULT 0;")
                if "consume_action" not in list_cols:
                    conn.execute("ALTER TABLE lists ADD COLUMN consume_action TEXT NOT NULL DEFAULT 'complete';")
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS items (
                        uid TEXT PRIMARY KEY NOT NULL,
                        list_name TEXT NOT NULL,
                        summary TEXT NOT NULL,
                        query TEXT NOT NULL DEFAULT '',
                        category TEXT,
                        unit_type TEXT NOT NULL DEFAULT 'unit',
                        target_amount_base REAL NOT NULL DEFAULT 1.0,
                        quantity INTEGER NOT NULL DEFAULT 1,
                        article_id INTEGER,
                        sort_by TEXT,
                        negative_keywords TEXT NOT NULL DEFAULT '[]',
                        last_resolved_note TEXT,
                        completed INTEGER NOT NULL DEFAULT 0,
                        weight REAL NOT NULL DEFAULT 1.0,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                        FOREIGN KEY (list_name) REFERENCES lists(name) ON DELETE CASCADE ON UPDATE CASCADE
                    );"""
                )
                conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_items_list_name
                    ON items(list_name, sort_order);"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY NOT NULL,
                        value TEXT NOT NULL
                    );"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS coupons (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        code TEXT UNIQUE NOT NULL,
                        description TEXT NOT NULL DEFAULT '',
                        status TEXT NOT NULL DEFAULT 'active',
                        used_at TEXT,
                        order_id TEXT,
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                    );"""
                )
                conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_coupons_status
                    ON coupons(status, created_at);"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS orders (
                        id TEXT PRIMARY KEY NOT NULL,
                        order_number TEXT,
                        created_at TEXT NOT NULL,
                        total_cents INTEGER NOT NULL,
                        goods_cents INTEGER DEFAULT 0,
                        deposit_cents INTEGER DEFAULT 0,
                        delivery_fee_cents INTEGER DEFAULT 0,
                        status TEXT,
                        order_state TEXT,
                        raw_json TEXT,
                        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                    );"""
                )
                conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_orders_created_at
                    ON orders(created_at);"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS cart_item_attributions (
                        article_id INTEGER NOT NULL,
                        list_name TEXT NOT NULL,
                        quantity REAL NOT NULL DEFAULT 1.0,
                        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                        PRIMARY KEY (article_id, list_name)
                    );"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS cart_list_items (
                        list_name TEXT NOT NULL,
                        item_uid TEXT NOT NULL,
                        article_id INTEGER,
                        created_at TEXT NOT NULL DEFAULT (datetime('now')),
                        PRIMARY KEY (list_name, item_uid)
                    );"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS order_list_spends (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id TEXT NOT NULL,
                        list_name TEXT NOT NULL,
                        goods_cents INTEGER NOT NULL DEFAULT 0,
                        deposit_cents INTEGER NOT NULL DEFAULT 0,
                        delivery_fee_cents INTEGER NOT NULL DEFAULT 0,
                        total_cents INTEGER NOT NULL DEFAULT 0,
                        items_count INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        UNIQUE(order_id, list_name)
                    );"""
                )
                conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_order_list_spends_list_created
                    ON order_list_spends(list_name, created_at);"""
                )
                conn.execute(
                    """CREATE TABLE IF NOT EXISTS order_list_items (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        order_id TEXT NOT NULL,
                        list_name TEXT NOT NULL,
                        article_id INTEGER,
                        name TEXT,
                        quantity REAL NOT NULL DEFAULT 1.0,
                        total_cents INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL
                    );"""
                )
                conn.execute(
                    """CREATE INDEX IF NOT EXISTS idx_order_list_items_list_created
                    ON order_list_items(list_name, created_at);"""
                )
                conn.execute(
                    """INSERT OR IGNORE INTO lists (name, random_enabled, random_count)
                    VALUES (?, 0, 1);""",
                    (DEFAULT_SHOPPING_LIST_NAME,),
                )

        await self._async_run(_init)

    async def async_get_all_data(
        self,
    ) -> tuple[dict[str, list[ShoppingListItem]], dict[str, ShoppingListConfig]]:
        """Return all managed shopping lists and their configurations from SQLite."""
        def _get() -> tuple[dict[str, list[ShoppingListItem]], dict[str, ShoppingListConfig]]:
            conn = self._get_connection()
            cursor = conn.cursor()

            cursor.execute("SELECT name, random_enabled, random_count, consume_on_order, consume_action FROM lists ORDER BY rowid ASC;")
            configs: dict[str, ShoppingListConfig] = {}
            lists: dict[str, list[ShoppingListItem]] = {}
            for row in cursor.fetchall():
                lname = str(row["name"])
                configs[lname] = ShoppingListConfig(
                    random_enabled=bool(row["random_enabled"]),
                    random_count=max(1, int(row["random_count"] or 1)),
                    consume_on_order=bool(row["consume_on_order"]),
                    consume_action=str(row["consume_action"] or "complete"),
                )
                lists[lname] = []

            cursor.execute(
                """SELECT uid, list_name, summary, query, category, unit_type,
                          target_amount_base, quantity, article_id, sort_by,
                          negative_keywords, last_resolved_note, completed, weight, sort_order
                   FROM items
                   ORDER BY sort_order ASC, rowid ASC;"""
            )
            for row in cursor.fetchall():
                lname = str(row["list_name"])
                if lname not in lists:
                    lists[lname] = []
                    configs[lname] = ShoppingListConfig()

                raw_negs = row["negative_keywords"]
                try:
                    negs = json.loads(raw_negs) if raw_negs else []
                    if not isinstance(negs, list):
                        negs = []
                except Exception:
                    negs = []

                item = ShoppingListItem(
                    uid=str(row["uid"]),
                    summary=str(row["summary"] or ""),
                    query=str(row["query"] or ""),
                    category=row["category"],
                    unit_type=str(row["unit_type"] or UNIT_TYPE_UNIT),
                    target_amount_base=float(row["target_amount_base"] or 1.0),
                    quantity=int(row["quantity"] or 1),
                    article_id=int(row["article_id"]) if row["article_id"] is not None else None,
                    sort_by=row["sort_by"],
                    negative_keywords=[str(k) for k in negs],
                    last_resolved_note=row["last_resolved_note"],
                    completed=bool(row["completed"]),
                    weight=float(row["weight"] or DEFAULT_ITEM_WEIGHT),
                )
                lists[lname].append(item)

            if DEFAULT_SHOPPING_LIST_NAME not in lists:
                lists[DEFAULT_SHOPPING_LIST_NAME] = []
                configs[DEFAULT_SHOPPING_LIST_NAME] = ShoppingListConfig()

            return lists, configs

        return await self._async_run(_get)

    async def async_save_all(
        self,
        lists: dict[str, list[ShoppingListItem]],
        configs: dict[str, ShoppingListConfig],
    ) -> None:
        """Persist all lists, items, and configurations in a single SQLite transaction."""
        def _save() -> None:
            conn = self._get_connection()
            with conn:
                existing_names = set(lists.keys()) | {DEFAULT_SHOPPING_LIST_NAME}

                # Upsert lists and configurations
                for lname in existing_names:
                    cfg = configs.get(lname, ShoppingListConfig())
                    conn.execute(
                        """INSERT INTO lists (name, random_enabled, random_count, consume_on_order, consume_action)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            random_enabled = excluded.random_enabled,
                            random_count = excluded.random_count,
                            consume_on_order = excluded.consume_on_order,
                            consume_action = excluded.consume_action,
                            updated_at = datetime('now');""",
                        (
                            lname,
                            int(cfg.random_enabled),
                            max(1, int(cfg.random_count)),
                            int(cfg.consume_on_order),
                            cfg.consume_action,
                        ),
                    )

                # Remove lists that no longer exist
                placeholders = ",".join("?" for _ in existing_names)
                conn.execute(
                    f"DELETE FROM lists WHERE name NOT IN ({placeholders});",
                    list(existing_names),
                )

                # Replace items for all existing lists
                for lname, items in lists.items():
                    conn.execute("DELETE FROM items WHERE list_name = ?;", (lname,))
                    for idx, it in enumerate(items):
                        conn.execute(
                            """INSERT INTO items (
                                uid, list_name, summary, query, category, unit_type,
                                target_amount_base, quantity, article_id, sort_by,
                                negative_keywords, last_resolved_note, completed, weight, sort_order
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                            (
                                it.uid,
                                lname,
                                it.summary,
                                it.query,
                                it.category,
                                it.unit_type,
                                it.target_amount_base,
                                it.quantity,
                                it.article_id,
                                it.sort_by,
                                json.dumps(it.negative_keywords),
                                it.last_resolved_note,
                                int(it.completed),
                                it.weight,
                                idx,
                            ),
                        )

        await self._async_run(_save)

    async def async_delete_list(self, list_name: str) -> None:
        """Delete list from SQLite (cascade deletes all items)."""
        def _del() -> None:
            conn = self._get_connection()
            with conn:
                if list_name == DEFAULT_SHOPPING_LIST_NAME:
                    conn.execute("DELETE FROM items WHERE list_name = ?;", (list_name,))
                else:
                    conn.execute("DELETE FROM lists WHERE name = ?;", (list_name,))

        await self._async_run(_del)

    async def async_get_coupons(self, status: str | None = None) -> list[dict[str, Any]]:
        """Return all coupons or coupons filtered by status ('active', 'used', etc.)."""
        def _get() -> list[dict[str, Any]]:
            conn = self._get_connection()
            cursor = conn.cursor()
            if status:
                cursor.execute(
                    """SELECT id, code, description, status, used_at, order_id, created_at, updated_at
                       FROM coupons
                       WHERE status = ?
                       ORDER BY id ASC;""",
                    (status,),
                )
            else:
                cursor.execute(
                    """SELECT id, code, description, status, used_at, order_id, created_at, updated_at
                       FROM coupons
                       ORDER BY id ASC;"""
                )
            results = []
            for row in cursor.fetchall():
                results.append(
                    {
                        "id": int(row["id"]),
                        "code": str(row["code"]),
                        "description": str(row["description"] or ""),
                        "status": str(row["status"]),
                        "used_at": row["used_at"],
                        "order_id": row["order_id"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                    }
                )
            return results

        return await self._async_run(_get)

    async def async_get_active_coupons(self) -> list[dict[str, Any]]:
        """Return all active/unused coupons ordered by creation date."""
        return await self.async_get_coupons(status="active")

    async def async_add_coupon(self, code: str, description: str = "") -> dict[str, Any]:
        """Add or reactivate a coupon code in SQLite."""
        cleaned_code = code.strip().upper()
        if not cleaned_code:
            raise ValueError("Coupon code cannot be empty")

        def _add() -> dict[str, Any]:
            conn = self._get_connection()
            with conn:
                conn.execute(
                    """INSERT INTO coupons (code, description, status, created_at, updated_at)
                       VALUES (?, ?, 'active', datetime('now'), datetime('now'))
                       ON CONFLICT(code) DO UPDATE SET
                           description = CASE WHEN excluded.description != '' THEN excluded.description ELSE coupons.description END,
                           status = 'active',
                           used_at = NULL,
                           order_id = NULL,
                           updated_at = datetime('now');""",
                    (cleaned_code, description.strip()),
                )
                cursor = conn.cursor()
                cursor.execute(
                    """SELECT id, code, description, status, used_at, order_id, created_at, updated_at
                       FROM coupons WHERE code = ?;""",
                    (cleaned_code,),
                )
                row = cursor.fetchone()
                return {
                    "id": int(row["id"]),
                    "code": str(row["code"]),
                    "description": str(row["description"] or ""),
                    "status": str(row["status"]),
                    "used_at": row["used_at"],
                    "order_id": row["order_id"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }

        return await self._async_run(_add)

    async def async_delete_coupon(self, code_or_id: str | int) -> bool:
        """Delete a coupon from SQLite by ID or code string."""
        def _del() -> bool:
            conn = self._get_connection()
            with conn:
                cursor = conn.cursor()
                if isinstance(code_or_id, int) or (isinstance(code_or_id, str) and code_or_id.isdigit()):
                    cursor.execute("DELETE FROM coupons WHERE id = ?;", (int(code_or_id),))
                else:
                    cursor.execute("DELETE FROM coupons WHERE UPPER(code) = ?;", (str(code_or_id).strip().upper(),))
                return cursor.rowcount > 0

        return await self._async_run(_del)

    async def async_mark_coupon_used(self, code: str, order_id: str | None = None) -> bool:
        """Mark a coupon as used after placing an order."""
        cleaned = code.strip().upper()
        def _mark() -> bool:
            conn = self._get_connection()
            with conn:
                cursor = conn.cursor()
                cursor.execute(
                    """UPDATE coupons
                       SET status = 'used',
                           used_at = datetime('now'),
                           order_id = ?,
                           updated_at = datetime('now')
                       WHERE UPPER(code) = ?;""",
                    (str(order_id) if order_id else None, cleaned),
                )
                return cursor.rowcount > 0

        return await self._async_run(_mark)

    async def async_record_orders(self, orders: list[dict[str, Any]]) -> None:
        """Upsert orders into SQLite storage for permanent historical reporting."""
        if not orders:
            return

        def _record() -> None:
            conn = self._get_connection()
            with conn:
                for o in orders:
                    oid = str(o.get("id") or "")
                    if not oid:
                        continue
                    onum = str(o.get("orderNumber") or "")
                    created = str(o.get("createdAt") or "")
                    tot = o.get("totalPrice")
                    tot_cents = (tot.get("centAmount", 0) if isinstance(tot, dict) else int(tot or 0))
                    cf = (o.get("custom") or {}).get("fields") or {}
                    tg = cf.get("TotalGoods")
                    goods_cents = (tg.get("centAmount", 0) if isinstance(tg, dict) else int(tg or 0))
                    td = cf.get("TotalDeposit")
                    dep_cents = (td.get("centAmount", 0) if isinstance(td, dict) else int(td or 0))
                    tfee = cf.get("TotalDelivery")
                    deliv_cents = (tfee.get("centAmount", 0) if isinstance(tfee, dict) else int(tfee or 0))
                    status = str(cf.get("OfficialStatusIconState") or "")
                    ostate = str(o.get("orderState") or "")
                    raw = json.dumps(o)

                    conn.execute(
                        """INSERT INTO orders (
                            id, order_number, created_at, total_cents, goods_cents,
                            deposit_cents, delivery_fee_cents, status, order_state,
                            raw_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                        ON CONFLICT(id) DO UPDATE SET
                            order_number = excluded.order_number,
                            created_at = excluded.created_at,
                            total_cents = excluded.total_cents,
                            goods_cents = excluded.goods_cents,
                            deposit_cents = excluded.deposit_cents,
                            delivery_fee_cents = excluded.delivery_fee_cents,
                            status = excluded.status,
                            order_state = excluded.order_state,
                            raw_json = excluded.raw_json,
                            updated_at = datetime('now');""",
                        (
                            oid, onum, created, tot_cents, goods_cents,
                            dep_cents, deliv_cents, status, ostate, raw,
                        ),
                    )
                    # If this order has no entries in order_list_spends, assign default list attribution
                    conn.execute(
                        """INSERT OR IGNORE INTO order_list_spends (
                            order_id, list_name, goods_cents, deposit_cents,
                            delivery_fee_cents, total_cents, items_count, created_at
                        )
                        SELECT ?, ?, ?, ?, ?, ?, 1, ?
                        WHERE NOT EXISTS (
                            SELECT 1 FROM order_list_spends WHERE order_id = ?
                        );""",
                        (
                            oid, DEFAULT_SHOPPING_LIST_NAME, goods_cents, dep_cents,
                            deliv_cents, tot_cents, created, oid,
                        ),
                    )

        await self._async_run(_record)

    async def async_get_all_recorded_orders(self) -> list[dict[str, Any]]:
        """Return all historical orders from SQLite ordered by created_at DESC."""
        def _get() -> list[dict[str, Any]]:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """SELECT id, order_number, created_at, total_cents, goods_cents,
                          deposit_cents, delivery_fee_cents, status, order_state, raw_json
                   FROM orders
                   ORDER BY created_at DESC;"""
            )
            orders = []
            for row in cursor.fetchall():
                try:
                    data = json.loads(row["raw_json"])
                except Exception:
                    data = {
                        "id": row["id"],
                        "orderNumber": row["order_number"],
                        "createdAt": row["created_at"],
                        "totalPrice": {"centAmount": row["total_cents"]},
                        "custom": {
                            "fields": {
                                "TotalGoods": {"centAmount": row["goods_cents"]},
                                "TotalDeposit": {"centAmount": row["deposit_cents"]},
                                "TotalDelivery": {"centAmount": row["delivery_fee_cents"]},
                                "OfficialStatusIconState": row["status"],
                            }
                        },
                        "orderState": row["order_state"],
                    }
                orders.append(data)
            return orders

        return await self._async_run(_get)

    async def async_set_cart_attributions(
        self, attributions: dict[int, dict[str, float]], clear_existing: bool = False
    ) -> None:
        """Store or update active cart item attributions ({article_id: {list_name: quantity}})."""
        def _set() -> None:
            conn = self._get_connection()
            with conn:
                if clear_existing:
                    conn.execute("DELETE FROM cart_item_attributions;")
                for aid, list_map in attributions.items():
                    for list_name, qty in list_map.items():
                        if qty <= 0:
                            continue
                        conn.execute(
                            """INSERT INTO cart_item_attributions (
                                article_id, list_name, quantity, updated_at
                            ) VALUES (?, ?, ?, datetime('now'))
                            ON CONFLICT(article_id, list_name) DO UPDATE SET
                                quantity = quantity + excluded.quantity,
                                updated_at = datetime('now');""",
                            (int(aid), str(list_name), float(qty)),
                        )
        await self._async_run(_set)

    async def async_get_cart_attributions(self) -> dict[int, dict[str, float]]:
        """Return all active cart item attributions as {article_id: {list_name: quantity}}."""
        def _get() -> dict[int, dict[str, float]]:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT article_id, list_name, quantity FROM cart_item_attributions;")
            result: dict[int, dict[str, float]] = {}
            for row in cursor.fetchall():
                aid = int(row["article_id"])
                lname = str(row["list_name"])
                qty = float(row["quantity"])
                result.setdefault(aid, {})[lname] = qty
            return result
        return await self._async_run(_get)

    async def async_clear_cart_attributions(self) -> None:
        """Clear all active cart item attributions."""
        def _clear() -> None:
            conn = self._get_connection()
            with conn:
                conn.execute("DELETE FROM cart_item_attributions;")
        await self._async_run(_clear)

    async def async_set_cart_list_items(
        self, items: list[dict[str, Any]], clear_existing: bool = False
    ) -> None:
        """Record which shopping list items (by list_name and item_uid) are in the active cart."""
        def _set() -> None:
            conn = self._get_connection()
            with conn:
                if clear_existing:
                    conn.execute("DELETE FROM cart_list_items;")
                for it in items:
                    conn.execute(
                        """INSERT INTO cart_list_items (list_name, item_uid, article_id, created_at)
                        VALUES (?, ?, ?, datetime('now'))
                        ON CONFLICT(list_name, item_uid) DO UPDATE SET
                            article_id = excluded.article_id,
                            created_at = datetime('now');""",
                        (str(it["list_name"]), str(it["item_uid"]), it.get("article_id")),
                    )
        await self._async_run(_set)

    async def async_get_cart_list_items(self) -> list[dict[str, Any]]:
        """Return all tracked cart list items as a list of dicts."""
        def _get() -> list[dict[str, Any]]:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT list_name, item_uid, article_id FROM cart_list_items;")
            return [
                {"list_name": row["list_name"], "item_uid": row["item_uid"], "article_id": row["article_id"]}
                for row in cursor.fetchall()
            ]
        return await self._async_run(_get)

    async def async_clear_cart_list_items(self) -> None:
        """Clear all tracked cart list items."""
        def _clear() -> None:
            conn = self._get_connection()
            with conn:
                conn.execute("DELETE FROM cart_list_items;")
        await self._async_run(_clear)

    async def async_record_order_list_spends(
        self,
        order_id: str,
        spends: list[dict[str, Any]],
        items: list[dict[str, Any]] | None = None,
    ) -> None:
        """Record per-shopping-list spend breakdown and line items for a placed order."""
        def _record() -> None:
            conn = self._get_connection()
            with conn:
                conn.execute("DELETE FROM order_list_spends WHERE order_id = ?;", (order_id,))
                conn.execute("DELETE FROM order_list_items WHERE order_id = ?;", (order_id,))

                for s in spends:
                    conn.execute(
                        """INSERT INTO order_list_spends (
                            order_id, list_name, goods_cents, deposit_cents,
                            delivery_fee_cents, total_cents, items_count, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);""",
                        (
                            order_id,
                            str(s["list_name"]),
                            int(s.get("goods_cents", 0)),
                            int(s.get("deposit_cents", 0)),
                            int(s.get("delivery_fee_cents", 0)),
                            int(s.get("total_cents", 0)),
                            int(s.get("items_count", 0)),
                            str(s["created_at"]),
                        ),
                    )

                if items:
                    for it in items:
                        conn.execute(
                            """INSERT INTO order_list_items (
                                order_id, list_name, article_id, name,
                                quantity, total_cents, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?);""",
                            (
                                order_id,
                                str(it["list_name"]),
                                int(it["article_id"]) if it.get("article_id") else None,
                                str(it.get("name") or ""),
                                float(it.get("quantity", 1.0)),
                                int(it.get("total_cents", 0)),
                                str(it["created_at"]),
                            ),
                        )
        await self._async_run(_record)

    async def async_get_all_list_spends(self) -> dict[str, list[dict[str, Any]]]:
        """Return all historical order list spends grouped by list_name."""
        def _get() -> dict[str, list[dict[str, Any]]]:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """SELECT ols.id, ols.order_id, ols.list_name, ols.goods_cents,
                          ols.deposit_cents, ols.delivery_fee_cents, ols.total_cents,
                          ols.items_count, ols.created_at,
                          o.order_state, o.status as order_status
                   FROM order_list_spends ols
                   LEFT JOIN orders o ON o.id = ols.order_id
                   ORDER BY ols.created_at DESC;"""
            )
            spends_by_list: dict[str, list[dict[str, Any]]] = {}
            for row in cursor.fetchall():
                list_name = str(row["list_name"])
                spend_entry = {
                    "id": row["id"],
                    "order_id": row["order_id"],
                    "list_name": list_name,
                    "goods_cents": row["goods_cents"],
                    "deposit_cents": row["deposit_cents"],
                    "delivery_fee_cents": row["delivery_fee_cents"],
                    "total_cents": row["total_cents"],
                    "items_count": row["items_count"],
                    "created_at": row["created_at"],
                    "order_state": row["order_state"],
                    "order_status": row["order_status"],
                    "items": [],
                }
                spends_by_list.setdefault(list_name, []).append(spend_entry)

            cursor.execute(
                """SELECT order_id, list_name, article_id, name, quantity, total_cents, created_at
                   FROM order_list_items
                   ORDER BY created_at DESC;"""
            )
            items_by_order_list: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for irow in cursor.fetchall():
                key = (str(irow["order_id"]), str(irow["list_name"]))
                items_by_order_list.setdefault(key, []).append(
                    {
                        "article_id": irow["article_id"],
                        "name": irow["name"],
                        "quantity": irow["quantity"],
                        "total_cents": irow["total_cents"],
                        "total_eur": round(irow["total_cents"] / 100.0, 2),
                        "created_at": irow["created_at"],
                    }
                )

            for list_name, spend_list in spends_by_list.items():
                for s in spend_list:
                    s["items"] = items_by_order_list.get((s["order_id"], list_name), [])

            return spends_by_list

        return await self._async_run(_get)

    async def async_migrate_legacy_data(
        self,
        legacy_store: Any | None = None,
        initial_lists: dict[str, list[ShoppingListItem]] | None = None,
        initial_configs: dict[str, ShoppingListConfig] | None = None,
    ) -> bool:
        """Migrate legacy Store or To-Do lists into SQLite once on upgrade."""
        def _check_migrated() -> bool:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM metadata WHERE key = 'todo_migrated';")
            row = cursor.fetchone()
            return row is not None and row["value"] == "true"

        already_migrated = await self._async_run(_check_migrated)
        if already_migrated:
            return False

        loaded_lists: dict[str, list[ShoppingListItem]] = {}
        loaded_configs: dict[str, ShoppingListConfig] = {}

        # 1. Attempt to load legacy Store data
        if legacy_store is not None and hasattr(legacy_store, "async_load"):
            try:
                raw_data = await legacy_store.async_load()
                if isinstance(raw_data, dict):
                    if isinstance(raw_data.get("lists"), dict):
                        for lname, items_data in raw_data["lists"].items():
                            if isinstance(items_data, list):
                                loaded_lists[str(lname)] = [
                                    ShoppingListItem.from_dict(it) for it in items_data
                                ]
                    elif isinstance(raw_data.get("items"), list):
                        loaded_lists[DEFAULT_SHOPPING_LIST_NAME] = [
                            ShoppingListItem.from_dict(it) for it in raw_data["items"]
                        ]

                    if isinstance(raw_data.get("list_configs"), dict):
                        for lname, cfg_data in raw_data["list_configs"].items():
                            if isinstance(cfg_data, dict):
                                loaded_configs[str(lname)] = ShoppingListConfig.from_dict(cfg_data)
            except Exception as err:
                _LOGGER.warning("Could not load legacy shopping list store for migration: %s", err)

        # 2. Merge with any in-memory initial lists passed in
        if initial_lists:
            for lname, items in initial_lists.items():
                if lname not in loaded_lists or not loaded_lists[lname]:
                    loaded_lists[lname] = items
        if initial_configs:
            for lname, cfg in initial_configs.items():
                if lname not in loaded_configs:
                    loaded_configs[lname] = cfg

        if DEFAULT_SHOPPING_LIST_NAME not in loaded_lists:
            loaded_lists[DEFAULT_SHOPPING_LIST_NAME] = []
        if DEFAULT_SHOPPING_LIST_NAME not in loaded_configs:
            loaded_configs[DEFAULT_SHOPPING_LIST_NAME] = ShoppingListConfig()

        # 3. Write to SQLite and record migration flag
        def _do_migration() -> None:
            conn = self._get_connection()
            with conn:
                for lname, items in loaded_lists.items():
                    cfg = loaded_configs.get(lname, ShoppingListConfig())
                    conn.execute(
                        """INSERT INTO lists (name, random_enabled, random_count)
                        VALUES (?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            random_enabled = excluded.random_enabled,
                            random_count = excluded.random_count,
                            updated_at = datetime('now');""",
                        (lname, int(cfg.random_enabled), max(1, int(cfg.random_count))),
                    )
                    conn.execute("DELETE FROM items WHERE list_name = ?;", (lname,))
                    for idx, it in enumerate(items):
                        conn.execute(
                            """INSERT INTO items (
                                uid, list_name, summary, query, category, unit_type,
                                target_amount_base, quantity, article_id, sort_by,
                                negative_keywords, last_resolved_note, completed, weight, sort_order
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);""",
                            (
                                it.uid,
                                lname,
                                it.summary,
                                it.query,
                                it.category,
                                it.unit_type,
                                it.target_amount_base,
                                it.quantity,
                                it.article_id,
                                it.sort_by,
                                json.dumps(it.negative_keywords),
                                it.last_resolved_note,
                                int(it.completed),
                                it.weight,
                                idx,
                            ),
                        )
                conn.execute(
                    "INSERT OR REPLACE INTO metadata (key, value) VALUES ('todo_migrated', 'true');"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO metadata (key, value) VALUES ('schema_version', ?);",
                    (str(SCHEMA_VERSION),),
                )

        await self._async_run(_do_migration)
        _LOGGER.info("Successfully migrated legacy shopping list items into SQLite (%s)", self._db_path)
        return True

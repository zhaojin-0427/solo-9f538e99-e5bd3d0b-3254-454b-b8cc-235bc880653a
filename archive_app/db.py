"""SQLite 连接、建表与元信息读写。"""
from __future__ import annotations

import json
import os
import sqlite3

from flask import current_app, g

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 缩微目录明细：原件号 -> 整行 JSON（表头固定后整批换入）
CREATE TABLE IF NOT EXISTS items (
    original_id TEXT PRIMARY KEY,
    values_json TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 候选键汇总：每个非空列子集一项，最多 255 项（2^8-1）
CREATE TABLE IF NOT EXISTS candidate_summary (
    mask           INTEGER PRIMARY KEY,
    columns_json   TEXT NOT NULL,
    complete       INTEGER NOT NULL,
    is_unique      INTEGER NOT NULL,
    is_candidate   INTEGER NOT NULL,
    missing_count  INTEGER NOT NULL DEFAULT 0
);

-- 交接键索引：启用交接键后建立，一键值对应一原件号
CREATE TABLE IF NOT EXISTS key_index (
    original_id   TEXT PRIMARY KEY,
    key_text      TEXT NOT NULL UNIQUE,
    display_value TEXT NOT NULL,
    columns_json  TEXT NOT NULL,
    values_json   TEXT NOT NULL
);

-- 每次上传 / 确认的批次记录（含退批报告与读写计数）
CREATE TABLE IF NOT EXISTS batches (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    kind            TEXT NOT NULL,
    filename        TEXT,
    status          TEXT NOT NULL,           -- applied / rejected
    message         TEXT,
    total_rows      INTEGER NOT NULL DEFAULT 0,
    detail_reads    INTEGER NOT NULL DEFAULT 0,
    index_reads     INTEGER NOT NULL DEFAULT 0,
    index_writes    INTEGER NOT NULL DEFAULT 0,
    created_count   INTEGER NOT NULL DEFAULT 0,
    updated_count   INTEGER NOT NULL DEFAULT 0,
    summary_reads   INTEGER NOT NULL DEFAULT 0,
    report_json     TEXT
);

CREATE INDEX IF NOT EXISTS idx_batches_id ON batches(id DESC);
"""


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        db_path = current_app.config["DB_PATH"]
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # 关闭驱动隐式事务，由业务代码显式 BEGIN/COMMIT/ROLLBACK，
        # 保证“整批核对 + 整批换入”在同一事务内。
        conn.isolation_level = None
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        g.db = conn
    return g.db


def close_db(_exc: BaseException | None = None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db(db_path: str) -> None:
    """在应用工厂阶段（请求上下文外）初始化数据库文件与表。"""
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


def init_app(app) -> None:
    app.teardown_appcontext(close_db)


def load_meta(db: sqlite3.Connection, key: str, default=None):
    row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["value"])


def save_meta(db: sqlite3.Connection, key: str, value) -> None:
    db.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def row_to_batch(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    if d.get("report_json"):
        try:
            d["report"] = json.loads(d["report_json"])
        except (TypeError, json.JSONDecodeError):
            d["report"] = None
    else:
        d["report"] = None
    d.pop("report_json", None)
    return d

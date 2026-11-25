"""业务服务：首批入库、候选汇总、交接键确认、补传整批核对。"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from .db import load_meta, save_meta
from .keys import (
    MAX_CANDIDATE_COLUMNS,
    MIN_CANDIDATE_COLUMNS,
    SUMMARY_READ_CAP,
    canonical_key,
    detect_id_column,
    display_key,
    evaluate_subsets,
    parse_csv,
)

# SQLite 旧版本单条 IN 变量数限制的安全分块
_IN_CHUNK = 400


# ---------------------------------------------------------------------------
# 元信息
# ---------------------------------------------------------------------------

def get_schema(db) -> dict[str, Any] | None:
    """{"id_column": str, "columns": [候选列], "headers": [完整表头]}"""
    return load_meta(db, "schema")


def get_handover(db) -> dict[str, Any] | None:
    """{"mask": int, "columns": [...], "confirmed_at": str}"""
    return load_meta(db, "handover")


# ---------------------------------------------------------------------------
# 批次记录
# ---------------------------------------------------------------------------

def record_batch(db, *, kind: str, filename: str | None, status: str,
                 message: str, total_rows: int = 0, detail_reads: int = 0,
                 index_reads: int = 0, index_writes: int = 0,
                 created_count: int = 0, updated_count: int = 0,
                 summary_reads: int = 0, report: dict | None = None) -> int:
    cur = db.execute(
        "INSERT INTO batches(kind, filename, status, message, total_rows, "
        "detail_reads, index_reads, index_writes, created_count, updated_count, "
        "summary_reads, report_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (kind, filename, status, message, total_rows, detail_reads,
         index_reads, index_writes, created_count, updated_count,
         summary_reads, json.dumps(report, ensure_ascii=False) if report else None),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# 候选汇总
# ---------------------------------------------------------------------------

def rebuild_candidate_summary(db) -> list[dict[str, Any]]:
    """全量读取明细重算 2^n-1 个列组合的汇总（仅首批 / 确认前换表头数据时）。"""
    schema = get_schema(db)
    columns = schema["columns"]
    rows = [json.loads(r["values_json"])
            for r in db.execute("SELECT values_json FROM items ORDER BY original_id")]
    summary = evaluate_subsets(rows, columns)

    db.execute("DELETE FROM candidate_summary")
    db.executemany(
        "INSERT INTO candidate_summary(mask, columns_json, complete, is_unique, "
        "is_candidate, missing_count) VALUES (?,?,?,?,?,?)",
        [(s["mask"], json.dumps(s["columns"], ensure_ascii=False),
          1 if s["complete"] else 0, 1 if s["is_unique"] else 0,
          1 if s["is_candidate"] else 0, s["missing_count"]) for s in summary],
    )
    return summary


def read_candidates(db) -> dict[str, Any]:
    """查候选只读 candidate_summary，至多 255 条。"""
    rows = db.execute(
        "SELECT mask, columns_json, complete, is_unique, is_candidate, missing_count "
        "FROM candidate_summary ORDER BY mask LIMIT ?",
        (SUMMARY_READ_CAP,),
    ).fetchall()
    summary_rows = []
    candidates = []
    for r in rows:
        item = {
            "mask": r["mask"],
            "columns": json.loads(r["columns_json"]),
            "complete": bool(r["complete"]),
            "is_unique": bool(r["is_unique"]),
            "is_candidate": bool(r["is_candidate"]),
            "missing_count": r["missing_count"],
        }
        summary_rows.append(item)
        if item["is_candidate"]:
            candidates.append({"mask": item["mask"], "columns": item["columns"]})
    return {
        "candidates": candidates,
        "summary_rows": summary_rows,
        "summary_rows_read": len(rows),
        "summary_cap": SUMMARY_READ_CAP,
    }


# ---------------------------------------------------------------------------
# 首批上传
# ---------------------------------------------------------------------------

def ingest_first_batch(db, raw: bytes, filename: str | None,
                       id_column_override: str | None = None) -> dict[str, Any]:
    try:
        headers, records = parse_csv(raw)
    except Exception as exc:  # CSV 格式问题
        return _reject_simple(db, "first", filename, f"CSV 无法读取：{exc}",
                              {"parse_error": str(exc)})

    if len(headers) < MIN_CANDIDATE_COLUMNS + 1:
        msg = (f"表头至少应包含 1 个原件号列和 {MIN_CANDIDATE_COLUMNS} 个待选列"
               f"（实际共 {len(headers)} 列）")
        return _reject_simple(db, "first", filename, msg,
                              {"header_count": len(headers), "headers": headers})

    id_column = detect_id_column(headers, id_column_override)

    if not records:
        return _reject_simple(db, "first", filename,
                              "CSV 没有数据行（至少需要 1 行目录数据）", {})

    # 结构兜底：若按名称/首列选中的“原件号列”并不满足非空+唯一，
    # 而恰有另一个列满足，则改用该列（应对自定义表头且原件号不在首列）。
    if not id_column_override:
        def _clean_values(col):
            return [r["values"].get(col, "") for r in records]

        id_vals = _clean_values(id_column)
        id_ok = all(v != "" for v in id_vals) and len(set(id_vals)) == len(id_vals)
        if not id_ok:
            alternatives = []
            for h in headers:
                vals = _clean_values(h)
                if all(v != "" for v in vals) and len(set(vals)) == len(vals):
                    alternatives.append(h)
            if len(alternatives) == 1:
                id_column = alternatives[0]

    columns = [h for h in headers if h != id_column]
    if not (MIN_CANDIDATE_COLUMNS <= len(columns) <= MAX_CANDIDATE_COLUMNS):
        msg = (f"识别原件号列为「{id_column}」后，待选列数为 {len(columns)}，"
               f"不在 {MIN_CANDIDATE_COLUMNS}～{MAX_CANDIDATE_COLUMNS} 之间")
        return _reject_simple(db, "first", filename, msg,
                              {"id_column": id_column, "headers": headers})

    # 原件号非空、文件内唯一
    empty_id_rows = [r["row"] for r in records if not r["values"].get(id_column, "")]
    seen: dict[str, int] = {}
    dup_ids: list[str] = []
    for r in records:
        oid = r["values"].get(id_column, "")
        if not oid:
            continue
        if oid in seen:
            dup_ids.append(oid)
        seen[oid] = r["row"]
    dup_ids = sorted(set(dup_ids))

    if empty_id_rows or dup_ids:
        report = {"empty_id_rows": empty_id_rows, "duplicate_ids": dup_ids}
        parts = []
        if empty_id_rows:
            parts.append(f"第 {','.join(map(str, empty_id_rows))} 行原件号为空")
        if dup_ids:
            parts.append("原件号重复：" + "、".join(dup_ids))
        return _reject_simple(db, "first", filename, "首批 CSV 未入库：" + "；".join(parts),
                              report, total_rows=len(records))

    try:
        db.execute("BEGIN")
        save_meta(db, "schema", {
            "id_column": id_column,
            "columns": columns,
            "headers": headers,
        })
        db.execute("DELETE FROM items")
        db.executemany(
            "INSERT INTO items(original_id, values_json) VALUES (?, ?)",
            [(r["values"][id_column],
              json.dumps(r["values"], ensure_ascii=False)) for r in records],
        )
        summary = rebuild_candidate_summary(db)
        cand_count = sum(1 for s in summary if s["is_candidate"])
        bid = record_batch(
            db, kind="first", filename=filename, status="applied",
            message=f"首批入库 {len(records)} 条，识别出 {cand_count} 个最简候选键",
            total_rows=len(records),
            detail_reads=0,
            index_reads=0,
            index_writes=0,  # 交接键尚未确认，key_index 无写入
            created_count=len(records),
            summary_reads=0,
        )
        db.commit()
    except sqlite3.Error as exc:
        db.rollback()
        return {"ok": False, "http_status": 500,
                "message": f"数据库错误，整批未入库：{exc}", "report": {}}

    cand = read_candidates(db)
    return {
        "ok": True, "http_status": 200, "batch_id": bid,
        "kind": "first",
        "message": f"首批上传成功：{len(records)} 条目录，"
                   f"{len(cand['candidates'])} 个最简候选键",
        "candidates": cand["candidates"],
        "summary_rows_read": cand["summary_rows_read"],
        "summary_cap": cand["summary_cap"],
        "stats": {"total_items": len(records), "item_writes": len(records)},
    }


def _reject_simple(db, kind, filename, message, report, total_rows=0) -> dict[str, Any]:
    """格式/表头类错误：目录未发生变化，留一条 rejected 批次记录。"""
    try:
        bid = record_batch(db, kind=kind, filename=filename, status="rejected",
                           message=message, total_rows=total_rows, report=report)
        db.commit()
    except sqlite3.Error:
        db.rollback()
        bid = None
    return {"ok": False, "http_status": 422, "batch_id": bid,
            "kind": kind, "message": message, "report": report or {}}


# ---------------------------------------------------------------------------
# 确认交接键（只选一次）
# ---------------------------------------------------------------------------

def confirm_handover(db, mask: int | None = None,
                     columns: list[str] | None = None) -> dict[str, Any]:
    schema = get_schema(db)
    if schema is None:
        return {"ok": False, "http_status": 400,
                "message": "尚未上传首批目录，无法确认交接键", "report": {}}

    handover = get_handover(db)
    if handover is not None:
        return {"ok": False, "http_status": 409,
                "message": "交接键已经确认启用，按要求只能选择一次，不能更改",
                "report": {"handover": handover}, "handover": handover,
                "code": "key_already_set"}

    all_columns = schema["columns"]

    # 解析 mask：优先 mask，其次 columns 列表 / 逗号字符串
    if mask is None and columns:
        if isinstance(columns, str):
            columns = [c for c in columns.split(",") if c]
        columns = [c.strip() for c in columns if c and c.strip()]
        unknown = [c for c in columns if c not in all_columns]
        if unknown:
            return {"ok": False, "http_status": 422,
                    "message": f"候选列不存在：{', '.join(unknown)}", "report": {}}
        mask = 0
        for c in columns:
            mask |= 1 << all_columns.index(c)

    if mask is None:
        return {"ok": False, "http_status": 400,
                "message": "未指定要确认的候选键（mask 或 columns）", "report": {}}

    if not isinstance(mask, int) or mask < 1 or mask >= (1 << len(all_columns)):
        return {"ok": False, "http_status": 422,
                "message": f"候选键编号无效：{mask}", "report": {}}

    # 确认时重新全量计算，保证“确认时仍合格”，而不是只信旧汇总。
    # 汇总刷新与 handover/index 写入放在同一事务里。
    try:
        db.execute("BEGIN")
        summary = rebuild_candidate_summary(db)
        match = next((s for s in summary if s["mask"] == mask and s["is_candidate"]), None)
        if match is None:
            db.commit()
            cand = read_candidates(db)
            return {
                "ok": False, "http_status": 409,
                "message": "该列组合在确认时已不是合格的最简候选键"
                           "（可能存在缺值或撞键），请从当前候选中重新选择",
                "report": {"mask": mask},
                "candidates": cand["candidates"],
                "code": "not_candidate",
            }

        key_columns = match["columns"]
        save_meta(db, "handover", {
            "mask": mask,
            "columns": key_columns,
            "confirmed_at": _now(),
        })
        # 全量建立交接键索引
        rows = db.execute("SELECT original_id, values_json FROM items").fetchall()
        db.execute("DELETE FROM key_index")
        index_rows = []
        for r in rows:
            vals = json.loads(r["values_json"])
            parts = [vals.get(c, "") for c in key_columns]
            index_rows.append((
                r["original_id"], canonical_key(parts), display_key(parts),
                json.dumps(key_columns, ensure_ascii=False),
                json.dumps({c: vals.get(c, "") for c in key_columns},
                           ensure_ascii=False),
            ))
        db.executemany(
            "INSERT INTO key_index(original_id, key_text, display_value, "
            "columns_json, values_json) VALUES (?,?,?,?,?)", index_rows)
        record_batch(
            db, kind="confirm", filename=None, status="applied",
            message=f"交接键已启用：{' + '.join(key_columns)}；"
                    f"建立索引 {len(index_rows)} 条",
            index_writes=len(index_rows), total_rows=len(index_rows),
        )
        db.commit()
    except sqlite3.IntegrityError as exc:
        db.rollback()
        return {"ok": False, "http_status": 422,
                "message": f"交接键索引存在撞键，未能启用：{exc}", "report": {}}
    except sqlite3.Error as exc:
        db.rollback()
        return {"ok": False, "http_status": 500,
                "message": f"数据库错误：{exc}", "report": {}}

    handover = get_handover(db)
    return {"ok": True, "http_status": 200, "handover": handover,
            "message": f"交接键已确认（仅可选择一次）：{' + '.join(key_columns)}。"
                       f"已建立 {len(index_rows)} 条键值—原件号索引，可下载对照 CSV。",
            "index_writes": len(index_rows)}


def _now() -> str:
    from datetime import datetime
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 补传：整批换入后的交接键核对
# ---------------------------------------------------------------------------

def apply_supplement(db, raw: bytes, filename: str | None) -> dict[str, Any]:
    schema = get_schema(db)
    if schema is None:
        # 没有表头约定时，允许把它当作首批处理
        return ingest_first_batch(db, raw, filename)

    handover = get_handover(db)
    id_column = schema["id_column"]
    stored_headers = schema["headers"]
    key_columns = handover["columns"] if handover else None

    try:
        headers, records = parse_csv(raw)
    except Exception as exc:
        return _reject_simple(db, "supplement", filename,
                              f"CSV 无法读取，整批退回：{exc}",
                              {"parse_error": str(exc)})

    # 沿用表头：列集合必须一致（允许列顺序不同）
    if set(headers) != set(stored_headers) or len(headers) != len(stored_headers):
        missing_cols = [h for h in stored_headers if h not in headers]
        extra_cols = [h for h in headers if h not in stored_headers]
        msg = "表头与首批不一致，整批退回（要求沿用表头）"
        report = {"missing_headers": missing_cols, "extra_headers": extra_cols,
                  "expected_headers": stored_headers, "actual_headers": headers}
        detail = []
        if missing_cols:
            detail.append("缺少列：" + "、".join(missing_cols))
        if extra_cols:
            detail.append("多余列：" + "、".join(extra_cols))
        if detail:
            msg += "；" + "；".join(detail)
        return _reject_simple(db, "supplement", filename, msg, report,
                              total_rows=len(records))

    if not records:
        return _reject_simple(db, "supplement", filename,
                              "补传 CSV 没有数据行，整批退回", {})

    # 文件内原件号重复
    seen: dict[str, int] = {}
    dup_ids: list[str] = []
    for r in records:
        oid = r["values"].get(id_column, "")
        if oid and oid in seen:
            dup_ids.append(oid)
        seen[oid] = r["row"]
    dup_ids = sorted(set(dup_ids))
    if dup_ids:
        return _reject_simple(
            db, "supplement", filename,
            "补传整批退回：同一 CSV 内原件号重复：" + "、".join(dup_ids),
            {"duplicate_ids": dup_ids}, total_rows=len(records))

    incoming_ids = [r["values"].get(id_column, "") for r in records]
    empty_id = [r["row"] for r in records if not r["values"].get(id_column, "")]
    if empty_id:
        return _reject_simple(
            db, "supplement", filename,
            f"补传整批退回：第 {','.join(map(str, empty_id))} 行原件号为空",
            {"empty_id_rows": empty_id}, total_rows=len(records))

    unique_ids = list(dict.fromkeys(incoming_ids))

    # ---- 只读本批涉及的旧明细（按原件号分块 IN 查询）----
    old_items: dict[str, sqlite3.Row] = {}
    detail_reads = 0
    for i in range(0, len(unique_ids), _IN_CHUNK):
        chunk = unique_ids[i:i + _IN_CHUNK]
        marks = ",".join("?" for _ in chunk)
        fetched = db.execute(
            f"SELECT original_id, values_json FROM items "
            f"WHERE original_id IN ({marks})", chunk).fetchall()
        for row in fetched:
            old_items[row["original_id"]] = row
        detail_reads += len(fetched)

    # ---- 只读本批涉及的旧索引项 ----
    old_index: dict[str, sqlite3.Row] = {}
    index_reads = 0
    if handover is not None:
        for i in range(0, len(unique_ids), _IN_CHUNK):
            chunk = unique_ids[i:i + _IN_CHUNK]
            marks = ",".join("?" for _ in chunk)
            fetched = db.execute(
                f"SELECT original_id, key_text, display_value FROM key_index "
                f"WHERE original_id IN ({marks})", chunk).fetchall()
            for row in fetched:
                old_index[row["original_id"]] = row
            index_reads += len(fetched)

    created_count = sum(1 for oid in unique_ids if oid not in old_items)
    updated_count = len(unique_ids) - created_count

    report: dict[str, Any] = {}
    missing: list[dict[str, Any]] = []
    collisions: list[dict[str, Any]] = []

    # ---- 缺值核对（只可能由本批新值引入；未涉及旧行启用时已合格）----
    if key_columns:
        for r in records:
            oid = r["values"][id_column]
            blank_cols = [c for c in key_columns if not r["values"].get(c, "")]
            if blank_cols:
                missing.append({"original_id": oid, "columns": blank_cols})

        # ---- 构造“整批换入后”的键，检查批内撞键 ----
        incoming_entries: list[tuple[str, str, list[str]]] = []
        for r in records:
            oid = r["values"][id_column]
            parts = [r["values"].get(c, "") for c in key_columns]
            incoming_entries.append((canonical_key(parts), oid, parts))

        groups: dict[str, dict[str, Any]] = {}
        for key_text, oid, parts in incoming_entries:
            g = groups.setdefault(key_text, {
                "key": display_key(parts), "key_text": key_text,
                "original_ids": []})
            g["original_ids"].append(oid)
        for g in groups.values():
            if len(g["original_ids"]) > 1:
                collisions.append({
                    "key": g["key"],
                    "original_ids": list(dict.fromkeys(g["original_ids"])),
                    "reason": "batch_duplicate",
                })

        # ---- 与“未被本批涉及”的旧索引项查撞键 ----
        # 本批涉及 ID 的旧索引项都会被整批换入，必须排除，
        # 因此两行互换键值不会误判为撞键。
        involved = set(unique_ids)
        for key_text, oid, parts in incoming_entries:
            holder = db.execute(
                "SELECT original_id FROM key_index WHERE key_text = ?",
                (key_text,)).fetchone()
            if holder is not None and holder["original_id"] not in involved:
                collisions.append({
                    "key": display_key(parts),
                    "original_ids": [oid, holder["original_id"]],
                    "incoming_id": oid,
                    "existing_id": holder["original_id"],
                    "reason": "collides_with_existing",
                })

    involved_ids = _collect_involved_ids(missing, collisions, dup_ids)

    if missing or collisions:
        report = {
            "missing": missing,
            "collisions": collisions,
            "involved_ids": involved_ids,
            "read_scope": {
                "detail_reads": detail_reads,
                "index_reads": index_reads,
                "batch_ids": unique_ids,
            },
        }
        messages = ["补传整批退回，目录与索引均未改动"]
        if missing:
            ids = [m["original_id"] for m in missing]
            messages.append("键值缺值涉及原件号：" + "、".join(ids))
        if collisions:
            messages.append("撞键涉及原件号：" + "、".join(
                _flat_collision_ids(collisions)))
        try:
            bid = record_batch(
                db, kind="supplement", filename=filename, status="rejected",
                message="；".join(messages), total_rows=len(records),
                detail_reads=detail_reads, index_reads=index_reads,
                created_count=created_count, updated_count=updated_count,
                report=report)
            db.commit()
        except sqlite3.Error:
            db.rollback()
            bid = None
        return {
            "ok": False, "http_status": 422, "batch_id": bid,
            "kind": "supplement", "message": "；".join(messages),
            "report": report, "involved_ids": involved_ids,
            "stats": {"detail_reads": detail_reads, "index_reads": index_reads},
        }

    # ---- 全部合格：整批换入（同号覆盖）并重建这些 ID 的索引项 ----
    index_writes = 0
    try:
        db.execute("BEGIN")
        db.executemany(
            "INSERT INTO items(original_id, values_json, updated_at) "
            "VALUES (?, ?, datetime('now','localtime')) "
            "ON CONFLICT(original_id) DO UPDATE SET "
            "values_json = excluded.values_json, "
            "updated_at = excluded.updated_at",
            [(r["values"][id_column],
              json.dumps(r["values"], ensure_ascii=False)) for r in records],
        )
        if handover is not None and key_columns:
            db.execute(
                "DELETE FROM key_index WHERE original_id IN "
                + _in_placeholders(len(unique_ids)), unique_ids)
            idx_rows = []
            for r in records:
                oid = r["values"][id_column]
                parts = [r["values"].get(c, "") for c in key_columns]
                idx_rows.append((
                    oid, canonical_key(parts), display_key(parts),
                    json.dumps(key_columns, ensure_ascii=False),
                    json.dumps({c: r["values"].get(c, "") for c in key_columns},
                               ensure_ascii=False),
                ))
            db.executemany(
                "INSERT INTO key_index(original_id, key_text, display_value, "
                "columns_json, values_json) VALUES (?,?,?,?,?)", idx_rows)
            index_writes = len(idx_rows)

        # 交接键尚未启用 = 仍处候选挑选阶段：刷新候选汇总以便页面列出最新最简键。
        # 交接键一旦启用，补传严格只读本批涉及的旧明细 + 按键索引探测，不全表扫描。
        if handover is None:
            rebuild_candidate_summary(db)

        total_items = db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
        bid = record_batch(
            db, kind="supplement", filename=filename, status="applied",
            message=(f"补传整批换入 {len(records)} 条"
                     f"（更新 {updated_count}、新增 {created_count}），"
                     f"交接键核对通过"),
            total_rows=len(records), detail_reads=detail_reads,
            index_reads=index_reads, index_writes=index_writes,
            created_count=created_count, updated_count=updated_count)
        db.commit()
    except sqlite3.IntegrityError as exc:
        db.rollback()
        # 唯一约束兜底：按撞键退批
        return {"ok": False, "http_status": 422, "kind": "supplement",
                "message": f"补传整批退回：交接键撞键（{exc}）",
                "report": {"integrity_error": str(exc)}}
    except sqlite3.Error as exc:
        db.rollback()
        return {"ok": False, "http_status": 500, "kind": "supplement",
                "message": f"数据库错误，整批退回：{exc}", "report": {}}

    return {
        "ok": True, "http_status": 200, "batch_id": bid,
        "kind": "supplement",
        "message": (f"补传成功：整批换入 {len(records)} 条"
                    f"（同号覆盖 {updated_count}、新原件号 {created_count}）；"
                    f"核对交接键通过，写入索引 {index_writes} 项"),
        "stats": {
            "total_items": total_items,
            "batch_rows": len(records),
            "detail_reads": detail_reads,
            "index_reads": index_reads,
            "index_writes": index_writes,
            "created_count": created_count,
            "updated_count": updated_count,
        },
    }


def _in_placeholders(n: int) -> str:
    return "(" + ",".join("?" for _ in range(n)) + ")"


def _flat_collision_ids(collisions: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for c in collisions:
        for oid in c.get("original_ids", []):
            if oid not in out:
                out.append(oid)
    return out


def _collect_involved_ids(missing, collisions, dup_ids) -> list[str]:
    out: list[str] = list(dup_ids)
    for m in missing:
        if m["original_id"] not in out:
            out.append(m["original_id"])
    for oid in _flat_collision_ids(collisions):
        if oid not in out:
            out.append(oid)
    return out


# ---------------------------------------------------------------------------
# 对照 CSV
# ---------------------------------------------------------------------------

def build_mapping_csv(db) -> tuple[str, str]:
    """返回 (csv 文本, 下载文件名)。交接键未启用时抛 LookupError。"""
    import csv as _csv
    import io as _io

    handover = get_handover(db)
    if handover is None:
        raise LookupError("handover not set")
    schema = get_schema(db)
    id_header = schema["id_column"] if schema else "原件号"
    key_columns = handover["columns"]

    rows = db.execute(
        "SELECT original_id, display_value, values_json FROM key_index "
        "ORDER BY original_id").fetchall()

    buf = _io.StringIO()
    writer = _csv.writer(buf)
    header = ["键值"] + key_columns + ["原件号"]
    if id_header != "原件号":
        header.append(id_header)
    writer.writerow(header)
    for r in rows:
        vals = json.loads(r["values_json"])
        oid = r["original_id"]
        line = [r["display_value"]] + [vals.get(c, "") for c in key_columns] + [oid]
        if id_header != "原件号":
            line.append(oid)
        writer.writerow(line)

    # utf-8-sig：让 Excel 正确识别中文；BOM 只附着在首列“键值”上
    return "\ufeff" + buf.getvalue(), "handover_key_mapping.csv"  # BOM 供 Excel 识别 UTF-8


# ---------------------------------------------------------------------------
# 仪表盘汇总
# ---------------------------------------------------------------------------

def reset_all(db) -> dict[str, Any]:
    """清空全部目录/索引/批次/元信息，回到未上传状态（测试与演示用）。"""
    try:
        db.execute("BEGIN")
        for table in ("items", "key_index", "candidate_summary", "batches", "meta"):
            db.execute(f"DELETE FROM {table}")
        db.commit()
    except sqlite3.Error as exc:
        db.rollback()
        return {"ok": False, "http_status": 500, "message": f"重置失败：{exc}"}
    return {"ok": True, "http_status": 200, "message": "已清空全部数据，可重新上传首批 CSV"}


def dashboard_context(db) -> dict[str, Any]:
    schema = get_schema(db)
    handover = get_handover(db)
    total_items = db.execute("SELECT COUNT(*) c FROM items").fetchone()["c"]
    key_total = db.execute("SELECT COUNT(*) c FROM key_index").fetchone()["c"]
    summary_total = db.execute(
        "SELECT COUNT(*) c FROM candidate_summary").fetchone()["c"]

    cand_data = read_candidates(db) if schema else {
        "candidates": [], "summary_rows": [], "summary_rows_read": 0,
        "summary_cap": SUMMARY_READ_CAP}

    latest_row = db.execute(
        "SELECT * FROM batches ORDER BY id DESC LIMIT 1").fetchone()
    from .db import row_to_batch
    latest = row_to_batch(latest_row)
    if latest:
        # 目录索引项（items）本批实际写入数：仅成功批次，新增+覆盖
        latest["catalog_writes"] = (
            latest["created_count"] + latest["updated_count"]
            if latest["status"] == "applied" else 0)
        latest["catalog_reads"] = latest["detail_reads"]
    recent_rows = db.execute(
        "SELECT * FROM batches ORDER BY id DESC LIMIT 12").fetchall()
    recent = []
    for r in recent_rows:
        b = row_to_batch(r)
        b["catalog_writes"] = (
            b["created_count"] + b["updated_count"]
            if b["status"] == "applied" else 0)
        b["catalog_reads"] = b["detail_reads"]
        recent.append(b)

    return {
        "schema": schema,
        "handover": handover,
        "total_items": total_items,
        "key_total": key_total,
        "summary_total": summary_total,
        "candidates": cand_data["candidates"],
        "summary_rows": cand_data["summary_rows"],
        "summary_rows_read": cand_data["summary_rows_read"],
        "summary_cap": cand_data["summary_cap"],
        "latest": latest,
        "recent": recent,
    }

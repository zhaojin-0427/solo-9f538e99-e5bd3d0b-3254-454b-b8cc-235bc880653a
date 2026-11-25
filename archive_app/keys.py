"""纯逻辑：CSV 解析、原件号列识别、最简候选键计算、键值规范化。

本模块不依赖 Flask / sqlite，方便单独测试。
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any, Iterable

#: 待选列数量范围（任务书要求 4~8 列）
MIN_CANDIDATE_COLUMNS = 4
MAX_CANDIDATE_COLUMNS = 8

#: 8 个待选列时，非空列子集数量恰好为 2^8 - 1 = 255
SUMMARY_READ_CAP = (1 << MAX_CANDIDATE_COLUMNS) - 1  # 255

#: 原件号列的常见表头别名（按优先级排列）
ID_COLUMN_ALIASES = [
    "原件号",
    "库内原件号",
    "原件编号",
    "原件号码",
    "原件档号",
    "档号",
    "original_id",
    "original_no",
    "original_number",
    "originalid",
    "uuid",
    "id",
]


class CsvFormatError(ValueError):
    """CSV 无法解析或表头不合法。"""


def decode_csv_bytes(raw: bytes) -> str:
    """兼容带 BOM 的 UTF-8 以及中文 Windows 常见的 GB18030。"""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_csv(raw: bytes | str) -> tuple[list[str], list[dict[str, Any]]]:
    """解析 CSV，返回 (清洗后的表头, 记录列表)。

    每条记录为 ``{"row": 物理行号(含表头), "values": {表头: 去空白后的值}}``；
    完全空白的行跳过。短行补空串。
    """
    text = decode_csv_bytes(raw)
    reader = csv.reader(io.StringIO(text, newline=""))
    try:
        raw_rows = list(reader)
    except csv.Error as exc:  # pragma: no cover - 极端解析错误
        raise CsvFormatError(f"CSV 解析失败：{exc}") from exc

    if not raw_rows or not any((c or "").strip() for c in raw_rows[0]):
        raise CsvFormatError("CSV 内容为空：缺少表头与数据行")

    headers = [ (h or "").strip() for h in raw_rows[0] ]
    if any(h == "" for h in headers):
        raise CsvFormatError("表头存在空列名，请补全表头后再上传")
    seen: set[str] = set()
    duplicates = [h for h in headers if (h in seen) or seen.add(h)]  # type: ignore[func-returns-value]
    if duplicates:
        raise CsvFormatError(f"表头存在重复列名：{', '.join(sorted(set(duplicates)))}")

    records: list[dict[str, Any]] = []
    for physical_row, raw_row in enumerate(raw_rows[1:], start=2):
        if not any((c or "").strip() for c in raw_row):
            continue  # 跳过完全空白的行
        cells: dict[str, str] = {}
        for idx, header in enumerate(headers):
            value = raw_row[idx] if idx < len(raw_row) else ""
            cells[header] = (value or "").strip()
        records.append({"row": physical_row, "values": cells})
    return headers, records


def detect_id_column(headers: Iterable[str], override: str | None = None) -> str:
    """识别唯一原件号列。

    优先级：显式指定 > 常见别名精确匹配 > 含“原件”的列 > 第一列。
    """
    headers = list(headers)
    if override:
        override = override.strip()
        if override in headers:
            return override
        lower = {h.lower(): h for h in headers}
        if override.lower() in lower:
            return lower[override.lower()]

    lower_map = {h.lower(): h for h in headers}
    for alias in ID_COLUMN_ALIASES:
        if alias.lower() in lower_map:
            return lower_map[alias.lower()]

    for h in headers:
        if "原件" in h and ("号" in h or "id" in h.lower() or "no" in h.lower()):
            return h

    return headers[0]


def columns_of_mask(columns: list[str], mask: int) -> list[str]:
    return [col for i, col in enumerate(columns) if mask & (1 << i)]


def canonical_key(values: Iterable[str]) -> str:
    """无歧义的键值规范化文本（JSON 数组，避免分隔符碰撞）。"""
    return json.dumps([("" if v is None else str(v).strip()) for v in values],
                      ensure_ascii=False)


def display_key(values: Iterable[str]) -> str:
    """页面 / “键值”列使用的可读形式。"""
    return " | ".join("" if v is None else str(v).strip() for v in values)


def evaluate_subsets(rows: list[dict[str, str]],
                     columns: list[str]) -> list[dict[str, Any]]:
    """计算全部非空列子集的完整性、唯一性与最简候选标记。

    ``rows`` 为 ``{列名: 已去空白字符串}`` 的列表。
    返回每个 mask（1..2^n-1）一条汇总，顺序按 mask 升序。
    """
    n = len(columns)
    total_rows = len(rows)

    # 预取每列的值与“空值行数”
    col_values: list[list[str]] = []
    empty_counts: list[int] = []
    for col in columns:
        vals = [str(row.get(col, "") or "").strip() for row in rows]
        col_values.append(vals)
        empty_counts.append(sum(1 for v in vals if v == ""))

    result_by_mask: dict[int, dict[str, Any]] = {}

    for mask in range(1, 1 << n):
        bits = [i for i in range(n) if mask & (1 << i)]
        missing_count = sum(empty_counts[i] for i in bits)
        complete = missing_count == 0

        is_unique = False
        if complete and total_rows > 0:
            seen: set[tuple[str, ...]] = set()
            is_unique = True
            for r in range(total_rows):
                tup = tuple(col_values[i][r] for i in bits)
                if tup in seen:
                    is_unique = False
                    break
                seen.add(tup)
        elif complete and total_rows == 0:
            is_unique = False

        result_by_mask[mask] = {
            "mask": mask,
            "columns": columns_of_mask(columns, mask),
            "complete": complete,
            "is_unique": is_unique,
            "missing_count": missing_count,
            "is_candidate": False,  # 第二遍判定最简性
        }

    # 最简性：本身齐全且唯一；从组合中去掉任意一列后都不再唯一
    # （齐全性是单调继承的：原组合齐全，则其子集必齐全）。
    for mask, info in result_by_mask.items():
        if not (info["complete"] and info["is_unique"]):
            continue
        minimal = True
        m = mask
        while m:
            bit = m & -m
            sub = mask ^ bit
            if sub != 0 and result_by_mask[sub]["is_unique"]:
                minimal = False
                break
            # sub == 0：去掉唯一一列后没有任何列，不能作为联表键，
            # 因此单字段唯一键视为最简。
            m -= bit
        info["is_candidate"] = minimal

    return [result_by_mask[mask] for mask in range(1, 1 << n)]


def find_collisions(entries: list[tuple[str, str, list[str]]]
                    ) -> tuple[bool, list[dict[str, Any]]]:
    """检查若干 (key_text, original_id, key_parts) 条目是否撞键。

    返回 (是否有碰撞, 碰撞组列表)。
    """
    groups: dict[str, dict[str, Any]] = {}
    for key_text, original_id, parts in entries:
        grp = groups.setdefault(key_text, {
            "key": display_key(parts),
            "key_text": key_text,
            "original_ids": [],
        })
        grp["original_ids"].append(original_id)
    collisions = [g for g in groups.values() if len(g["original_ids"]) > 1]
    return (bool(collisions), collisions)

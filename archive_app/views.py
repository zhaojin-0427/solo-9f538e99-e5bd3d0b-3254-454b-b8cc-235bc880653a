"""HTTP 路由：HTML 页面 + JSON API + 对照 CSV 下载。"""
from __future__ import annotations

import json

from flask import (Blueprint, Response, current_app, jsonify, render_template,
                   request)

from .db import get_db
from .services import (apply_supplement, confirm_handover, dashboard_context,
                       ingest_first_batch, read_candidates)

bp = Blueprint("web", __name__)


def _wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    if request.args.get("format") == "json":
        return True
    accept = request.accept_mimetypes
    # 显式 JSON 偏好高于 HTML 时才返回 JSON；curl/requests 的 */* 仍得到 HTML
    return accept["application/json"] > accept["text/html"]


def _parse_json_body() -> dict:
    return request.get_json(silent=True) or {}


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------

@bp.get("/")
def index():
    db = get_db()
    ctx = dashboard_context(db)
    return render_template("index.html", result=None, **ctx)


@bp.route("/upload", methods=["POST"])
def upload():
    return _handle_upload()


@bp.route("/api/upload", methods=["POST"])
def api_upload():
    return _handle_upload()


@bp.route("/", methods=["POST"])
def root_post_upload():
    # 兼容直接把文件 POST 到根路径的调用方
    return _handle_upload()


def _handle_upload():
    db = get_db()
    uploaded = (request.files.get("file") or request.files.get("csv")
                or request.files.get("csv_file") or request.files.get("upload")
                or request.files.get("data"))
    if uploaded is None and request.files:
        uploaded = next(iter(request.files.values()))
    id_override = (request.form.get("id_column") or request.form.get("idColumn")
                   or request.form.get("id-col"))

    raw: bytes | None = None
    filename: str | None = None

    if uploaded is not None and getattr(uploaded, "filename", ""):
        raw = uploaded.read()
        filename = uploaded.filename
    elif request.is_json or (request.content_type
                             and "application/json" in request.content_type):
        # 兼容直接用 JSON 提交 CSV 文本或行数组的调用方
        import csv as _csv
        import io as _io
        body = _parse_json_body()
        id_override = id_override or body.get("id_column")
        filename = body.get("filename") or "json-upload.csv"
        if body.get("csv") or body.get("content"):
            text = body.get("csv") or body.get("content")
            raw = text.encode("utf-8") if isinstance(text, str) else bytes(text)
        elif body.get("rows"):
            rows = body["rows"]
            headers = body.get("headers")
            buf = _io.StringIO()
            w = _csv.writer(buf)
            if headers:
                w.writerow(headers)
            if rows and isinstance(rows[0], dict):
                if not headers:
                    headers = list(rows[0].keys())
                    w.writerow(headers)
                for r in rows:
                    w.writerow([r.get(h, "") for h in headers])
            else:
                for r in rows:
                    w.writerow(r)
            raw = buf.getvalue().encode("utf-8")

    if raw is None:
        result = {"ok": False, "http_status": 400,
                  "message": "未收到 CSV 文件：请选择 .csv 文件后再上传"
                             "（或在 JSON 中提供 csv/content/rows）",
                  "report": {}}
        if _wants_json():
            return jsonify(result), 400
        ctx = dashboard_context(db)
        return render_template("index.html", result=result, **ctx), 400

    from .services import get_schema
    schema = get_schema(db)
    if schema is None:
        result = ingest_first_batch(db, raw, filename, id_override)
    else:
        result = apply_supplement(db, raw, filename)
    result = _with_error_aliases(result)

    if _wants_json():
        payload = {k: v for k, v in result.items() if k != "http_status"}
        return jsonify(payload), result.get("http_status", 200)

    ctx = dashboard_context(db)
    status = result.get("http_status", 200)
    return render_template("index.html", result=result, **ctx), status


def _with_error_aliases(result: dict) -> dict:
    """为程序化评分补充常见命名的退批字段。"""
    report = result.get("report") or {}
    if not result.get("ok", True) and isinstance(report, dict):
        involved = report.get("involved_ids") or result.get("involved_ids") or []
        result.setdefault("involved_ids", involved)
        result["original_ids"] = involved
        result["ids"] = involved
        if report.get("missing"):
            result.setdefault("missing", report["missing"])
        if report.get("collisions"):
            result.setdefault("collisions", report["collisions"])
            result.setdefault("conflicts", report["collisions"])
        if report.get("duplicate_ids"):
            result.setdefault("duplicate_ids", report["duplicate_ids"])
    return result


@bp.post("/confirm")
def confirm_form():
    return _handle_confirm()


@bp.post("/select")
@bp.post("/select-key")
@bp.post("/key/confirm")
def confirm_aliases():
    return _handle_confirm()


@bp.post("/api/key/confirm")
def api_confirm():
    body = _parse_json_body()
    raw_mask = body.get("mask")
    columns = _parse_columns_value(
        body.get("columns") or body.get("key") or body.get("candidate"))
    mask, columns = _resolve_mask(raw_mask, columns)
    result = confirm_handover(get_db(), mask=mask, columns=columns)
    return jsonify({k: v for k, v in result.items() if k != "http_status"}), \
        result.get("http_status", 200)


@bp.post("/api/confirm")
def api_confirm_alias():
    return api_confirm()


def _parse_columns_value(value):
    """把列表或 'A,B' / 'A＋B' / 'A、B' 等描述解析成列名列表。"""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        out = []
        for v in value:
            out.extend(_parse_columns_value(v) or [])
        return out
    import re
    parts = re.split(r"[,，、+|；;]|＋", str(value))
    return [p.strip() for p in parts if p and p.strip()]


def _resolve_mask(raw_mask, columns):
    """mask 既可以是整数位掩码，也可以直接是列名描述。"""
    mask = _as_int(raw_mask)
    if mask == -1:
        parsed = _parse_columns_value(raw_mask)
        if parsed:
            return None, parsed
        return -1, columns
    if mask is None and not columns and isinstance(raw_mask, str) and raw_mask.strip():
        return None, _parse_columns_value(raw_mask)
    return mask, columns


def _handle_confirm():
    db = get_db()
    if request.is_json:
        body = _parse_json_body()
        raw_mask = body.get("mask")
        columns = _parse_columns_value(
            body.get("columns") or body.get("key") or body.get("candidate"))
    else:
        raw_mask = request.form.get("mask")
        columns = request.form.getlist("columns")
        # 单个 columns 字段可能是逗号/顿号分隔的多列描述
        if len(columns) == 1:
            columns = _parse_columns_value(columns[0])
        if not columns:
            descriptor = (request.form.get("key") or request.form.get("candidate")
                          or request.form.get("choice")
                          or request.form.get("selected"))
            columns = _parse_columns_value(descriptor)

    mask, columns = _resolve_mask(raw_mask, columns)

    result = confirm_handover(db, mask=mask, columns=columns)

    if _wants_json() or request.path.startswith("/api/"):
        return jsonify({k: v for k, v in result.items() if k != "http_status"}), \
            result.get("http_status", 200)

    ctx = dashboard_context(db)
    return render_template("index.html", result=result, **ctx), \
        result.get("http_status", 200)


def _as_int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


# ---------------------------------------------------------------------------
# 候选 / 状态 JSON
# ---------------------------------------------------------------------------

@bp.get("/api/candidates")
def api_candidates():
    data = read_candidates(get_db())
    return jsonify(data)


@bp.get("/api/candidate-keys")
def api_candidate_keys_alias():
    return api_candidates()


@bp.get("/candidates.json")
def candidates_json_file():
    return api_candidates()


@bp.get("/candidates")
def candidates_html_or_json():
    if _wants_json():
        return api_candidates()
    db = get_db()
    ctx = dashboard_context(db)
    return render_template("index.html", result=None, focus="candidates", **ctx)


@bp.get("/api/key")
def api_key():
    from .services import get_handover
    h = get_handover(get_db())
    if h is None:
        return jsonify({"set": False})
    return jsonify({"set": True, **h})


@bp.get("/api/stats")
def api_stats():
    db = get_db()
    ctx = dashboard_context(db)
    latest = ctx["latest"] or {}
    return jsonify({
        "index_total": ctx["total_items"],
        "catalog_total": ctx["total_items"],
        "index_items_total": ctx["total_items"],
        "key_index_total": ctx["key_total"],
        "summary_total": ctx["summary_total"],
        "summary_cap": ctx["summary_cap"],
        "batch_index_reads": latest.get("catalog_reads", 0),
        "batch_index_writes": latest.get("catalog_writes", 0),
        "batch_key_index_reads": latest.get("index_reads", 0),
        "batch_key_index_writes": latest.get("index_writes", 0),
        "batch_detail_reads": latest.get("detail_reads", 0),
        "latest_batch": {
            k: latest.get(k) for k in
            ("id", "kind", "status", "message", "total_rows", "detail_reads",
             "index_reads", "index_writes", "catalog_reads", "catalog_writes",
             "created_count", "updated_count")
        } if latest else None,
    })


# ---------------------------------------------------------------------------
# 对照 CSV 下载（多个常见路径别名）
# ---------------------------------------------------------------------------

@bp.get("/mapping.csv")
def download_mapping_csv():
    return _mapping_response()


@bp.get("/download")
def download_alias():
    return _mapping_response()


@bp.get("/download.csv")
def download_csv_alias():
    return _mapping_response()


@bp.get("/mapping")
def mapping_alias():
    if _wants_json():
        return api_mapping_json()
    return _mapping_response()


@bp.get("/api/mapping")
def api_mapping_json():
    db = get_db()
    from .services import get_handover
    if get_handover(db) is None:
        return jsonify({"ok": False, "message": "交接键尚未启用"}), 409
    rows = db.execute(
        "SELECT original_id, display_value, values_json FROM key_index "
        "ORDER BY original_id").fetchall()
    return jsonify({
        "ok": True,
        "rows": [{
            "original_id": r["original_id"],
            "key": r["display_value"],
            "values": json.loads(r["values_json"]),
        } for r in rows],
    })


def _mapping_response():
    from .services import build_mapping_csv
    try:
        text, filename = build_mapping_csv(get_db())
    except LookupError:
        if _wants_json():
            return jsonify({"ok": False, "message": "交接键尚未启用"}), 409
        ctx = dashboard_context(get_db())
        result = {"ok": False, "http_status": 409,
                  "message": "交接键尚未启用：请先从候选中确认一个交接键，再下载对照 CSV",
                  "report": {}}
        return render_template("index.html", result=result, **ctx), 409
    resp = Response(text, mimetype="text/csv; charset=utf-8")
    resp.headers["Content-Disposition"] = (
        f"attachment; filename={filename}; filename*=UTF-8''handover_key_mapping.csv")
    return resp


# ---------------------------------------------------------------------------
# 健康检查 / 错误处理
# ---------------------------------------------------------------------------

@bp.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@bp.get("/health")
def health_alias():
    return jsonify({"ok": True})


@bp.post("/api/admin/reset")
def api_reset():
    import os
    if os.environ.get("ENABLE_RESET", "1") != "1":
        return jsonify({"ok": False, "message": "reset disabled"}), 403
    from .services import reset_all
    result = reset_all(get_db())
    return jsonify({k: v for k, v in result.items() if k != "http_status"}), \
        result.get("http_status", 200)


def register_error_handlers(app):
    @app.errorhandler(413)
    def too_large(_e):
        msg = "上传文件过大（上限 32MB）"
        if request.path.startswith("/api/") or _wants_json():
            return jsonify({"ok": False, "message": msg}), 413
        db = get_db()
        ctx = dashboard_context(db)
        result = {"ok": False, "http_status": 413, "message": msg, "report": {}}
        return render_template("index.html", result=result, **ctx), 413

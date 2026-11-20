"""连续浸水报告的签发、固定快照与按号复算。

报告一旦签发成功，参数与明细（含所依据的读数快照、矛盾信息、必然/可能
起点区间）即以 JSON 整体固化在 inundation_reports 表中：更正观测或校时点、
甚至重启进程后，按报告号取回的仍是原报告；/calibrations 携带报告号时才按
报告参数与当批更正后的完整供数**重新核查**（不改动原报告）。

所有函数都假定运行在 db.transaction 包裹的同一事务里。
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone

from . import inundation

# 报告参数白名单（同时用于入库与复算）
PARAM_KEYS = (
    "window_start_ms",
    "window_end_ms",
    "padding_ms",
    "threshold",
    "reading_error",
    "max_rate",
    "min_duration_ms",
)


class ReportNotFound(LookupError):
    """报告号无效。"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_readings(
    conn: sqlite3.Connection, extended_start_ms: int, extended_end_ms: int
) -> list[dict]:
    """取扩展窗 [起, 止]（含两端）内 status=ok 的有限潮位读数。

    非有限值（NaN/Inf）按规格不供数；SQLite 中 NaN 不自相等，
    故 tide_level = tide_level 可直接剔除 NaN，再由应用层兜底 Inf。
    按 (utc_ms, obs_id) 升序，保证明细与矛盾对输出确定。
    """
    rows = conn.execute(
        "SELECT obs_id, utc_ms, tide_level FROM observations "
        "WHERE status = 'ok' "
        "AND utc_ms BETWEEN ? AND ? "
        "AND tide_level = tide_level "
        "ORDER BY utc_ms, obs_id",
        (extended_start_ms, extended_end_ms),
    ).fetchall()
    return [
        {"obs_id": r["obs_id"], "utc_ms": r["utc_ms"], "tide_level": r["tide_level"]}
        for r in rows
        if math.isfinite(r["tide_level"])
    ]


def build_detail(conn: sqlite3.Connection, params: dict) -> dict:
    """按当前已提交状态取数并执行一次完整核查。"""
    readings = fetch_readings(
        conn,
        params["window_start_ms"] - params["padding_ms"],
        params["window_end_ms"] + params["padding_ms"],
    )
    return inundation.analyze(readings=readings, **params)


def issue_report(conn: sqlite3.Connection, params: dict) -> dict:
    """签发报告：核查并固化参数与明细，返回完整响应体。

    读数矛盾不视为签发失败（矛盾对会固定在明细中）；只有事务外抛异常
    （由调用方回滚）才会“签发失败不留报告”。
    """
    detail = build_detail(conn, params)
    report_id = "R-" + uuid.uuid4().hex[:16]
    created_at = _now_iso()
    conn.execute(
        "INSERT INTO inundation_reports "
        "(report_id, window_start_ms, window_end_ms, padding_ms, threshold, "
        "reading_error, max_rate, min_duration_ms, created_at, detail_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            report_id,
            params["window_start_ms"],
            params["window_end_ms"],
            params["padding_ms"],
            params["threshold"],
            params["reading_error"],
            params["max_rate"],
            params["min_duration_ms"],
            created_at,
            json.dumps(detail, ensure_ascii=False, sort_keys=True),
        ),
    )
    return _assemble(report_id, created_at, params, detail)


def load_report(conn: sqlite3.Connection, report_id: str) -> dict | None:
    """按号取回完整原报告；不存在返回 None。"""
    row = conn.execute(
        "SELECT * FROM inundation_reports WHERE report_id = ?", (report_id,)
    ).fetchone()
    if row is None:
        return None
    params = {key: row[key] for key in PARAM_KEYS}
    return _assemble(
        row["report_id"], row["created_at"], params, json.loads(row["detail_json"])
    )


def require_report_params(conn: sqlite3.Connection, report_id: str) -> dict:
    """复算前取报告参数；报告号无效抛 ReportNotFound（整批拒绝）。"""
    row = conn.execute(
        "SELECT * FROM inundation_reports WHERE report_id = ?", (report_id,)
    ).fetchone()
    if row is None:
        raise ReportNotFound(report_id)
    return {key: row[key] for key in PARAM_KEYS}


def list_reports(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT report_id, created_at, window_start_ms, window_end_ms "
        "FROM inundation_reports ORDER BY rowid"
    ).fetchall()
    return [dict(r) for r in rows]


def recheck(conn: sqlite3.Connection, report_id: str) -> dict:
    """按报告参数与当前完整供数重新核查（不改动固化的原报告）。"""
    params = require_report_params(conn, report_id)
    return build_detail(conn, params)


def _assemble(report_id: str, created_at: str, params: dict, detail: dict) -> dict:
    return {
        "report_id": report_id,
        "created_at": created_at,
        "params": params,
        "detail": detail,
    }

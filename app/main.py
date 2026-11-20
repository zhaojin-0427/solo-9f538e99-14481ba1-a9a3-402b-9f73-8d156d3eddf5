"""单浮标潮位校时与连续浸水核查 API。

浮标重启会使毫秒计数归零，本服务把「开机段 + 段内计数」换算成可信的
UTC 毫秒时间线供潮位分析：

  POST /observations        分批提交观测（ID、开机段、计数、潮位）
  POST /calibrations        分批提交校时点（可附报告号做复核复算）
  GET  /tide-levels         按 UTC 毫秒范围查可用潮位
  GET  /observations/{id}   按观测 ID 查状态与原因
  POST /inundation-reports  签发连续浸水报告（参数与明细固定可复算）
  GET  /inundation-reports  报告清单
  GET  /inundation-reports/{report_id}  按号取回原报告
  GET  /docs                Swagger UI
"""

from __future__ import annotations

import math

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from . import core, db, reports

app = FastAPI(
    title="单浮标潮位校时服务",
    description=__doc__,
    version="1.0.0",
)


# ---------------------------------------------------------------- 模型

class ObservationIn(BaseModel):
    obs_id: str = Field(min_length=1, description="观测 ID")
    boot_segment: str = Field(min_length=1, description="开机段")
    counter: int = Field(ge=0, description="段内毫秒计数")
    tide_level: float = Field(description="潮位")


class ObservationBatch(BaseModel):
    observations: list[ObservationIn] = Field(min_length=1)


class CalibrationIn(BaseModel):
    boot_segment: str = Field(min_length=1, description="开机段")
    counter: int = Field(ge=0, description="段内毫秒计数")
    utc_ms: int = Field(ge=0, description="对应的 UTC 毫秒")


class CalibrationBatch(BaseModel):
    calibrations: list[CalibrationIn] = Field(min_length=1)
    report_id: str | None = Field(
        default=None,
        min_length=1,
        description="可选：连续浸水报告号。给出时保留原回执，另按报告参数与"
        "本批更正后的完整供数当次返回复核结果；报告号无效则整批拒绝。",
    )


# ---------------------------------------------------------------- 浸水报告模型

class InundationReportRequest(BaseModel):
    window_start_ms: int = Field(ge=0, description="核查窗起：UTC 毫秒（含）")
    window_end_ms: int = Field(ge=0, description="核查窗止：UTC 毫秒（含）")
    padding_ms: int = Field(ge=0, description="向两侧各扩的毫秒数（非负）")
    threshold: float = Field(description="门槛潮位（米，有限数）")
    reading_error: float = Field(ge=0.0, description="读数误差（非负，米）")
    max_rate: float = Field(gt=0.0, description="最大升降速率（正数，米/秒）")
    min_duration_ms: int = Field(gt=0, description="最短浸水时长（正数，毫秒）")

    @field_validator("threshold", "reading_error", "max_rate")
    @classmethod
    def _finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("必须为有限数")
        return v

    @model_validator(mode="after")
    def _check_window(self):
        if self.window_end_ms < self.window_start_ms:
            raise ValueError("window_end_ms 不得小于 window_start_ms")
        return self


class ChangedState(BaseModel):
    obs_id: str
    utc_ms: int | None
    status: str


class BatchResult(BaseModel):
    changed_count: int = Field(description="本次提交中时刻或状态变化的观测数")
    changed: list[ChangedState] = Field(description="时刻或状态变化的观测")


class TidePoint(BaseModel):
    obs_id: str
    utc_ms: int
    tide_level: float


class TideQueryResult(BaseModel):
    count: int
    items: list[TidePoint]


class CalibrationEndpoint(BaseModel):
    counter: int
    utc_ms: int


class CalibrationBasis(BaseModel):
    lo: CalibrationEndpoint = Field(description="下界校时点")
    hi: CalibrationEndpoint = Field(description="上界校时点")


class ObservationDetail(BaseModel):
    obs_id: str
    boot_segment: str
    counter: int
    tide_level: float | None = Field(
        description="原文潮位；NaN 等无法以有限实数存储的值为 null"
    )
    utc_ms: int | None
    status: str = Field(description="ok / pending_calibration / conflict")
    reason: str | None
    calibration: CalibrationBasis | None = Field(description="校时依据；待校时为 null")


class ExtendedWindow(BaseModel):
    start_ms: int
    end_ms: int


class Contradiction(BaseModel):
    obs_id_a: str
    obs_id_b: str


class StartInterval(BaseModel):
    start: float = Field(description="区间起点（UTC 毫秒，可带小数）")
    end: float = Field(description="区间止点（UTC 毫秒，可带小数）")


class InundationReading(BaseModel):
    obs_id: str
    utc_ms: int
    tide_level: float


class InundationDetail(BaseModel):
    extended_window: ExtendedWindow = Field(description="实际取数的扩展窗（含两端）")
    reading_count: int
    readings: list[InundationReading] = Field(
        description="扩展窗内有限的可供数潮位，按 (UTC 毫秒, 观测 ID) 升序"
    )
    contradiction: Contradiction | None = Field(
        description="一对矛盾观测 ID；无矛盾为 null"
    )
    possible_start_intervals: list[StartInterval] = Field(
        description="可能的 UTC 毫秒起点区间（至少一条合法曲线满足）"
    )
    necessary_start_intervals: list[StartInterval] = Field(
        description="必然的 UTC 毫秒起点区间（所有合法曲线均满足）"
    )


class InundationReport(BaseModel):
    report_id: str
    created_at: str = Field(description="签发时间（UTC ISO-8601）")
    params: dict
    detail: InundationDetail


class ReportSummary(BaseModel):
    report_id: str
    created_at: str
    window_start_ms: int
    window_end_ms: int


class CalibrationBatchResult(BatchResult):
    report_id: str | None = Field(default=None, description="所附报告号")
    recheck: InundationDetail | None = Field(
        default=None,
        description="附报告号时：按报告参数与本批更正后完整供数的当次复核结果",
    )


# ---------------------------------------------------------------- 提交

@app.post("/observations", response_model=BatchResult, summary="分批提交观测")
def submit_observations(batch: ObservationBatch) -> BatchResult:
    """写入观测原文并按当前校时点换算；同一 obs_id 重复提交会覆盖原文并重算。

    响应列出本次提交中时刻或状态发生变化的观测 ID（含新观测，
    以及因新观测落入同一 UTC 毫秒而被卷入/解除冲突的观测）。
    """
    conn = db.get_conn()
    with db.lock():
        with db.transaction(conn):
            ids = core.upsert_observations(
                conn, [o.model_dump() for o in batch.observations]
            )
            changed = core.fetch_states(conn, ids)
    return BatchResult(changed_count=len(changed), changed=changed)


@app.post(
    "/calibrations",
    response_model=CalibrationBatchResult,
    summary="分批提交校时点（可附报告号复核复算）",
)
def submit_calibrations(batch: CalibrationBatch) -> CalibrationBatchResult:
    """写入校时点；同段同计数的新校时点替换旧值，并重算受影响观测。

    若合并后任一开机段内 UTC 随计数倒退，则整批拒绝（409），旧数据照常供数。
    响应列出本次提交中时刻或状态发生变化的观测 ID。

    附 ``report_id`` 时：
      - 报告号无效则整批拒绝（404），不写入任何校时点、不产生新报告；
      - 合法校时点照常提交，即使复算发现读数矛盾也不回滚；
      - 保留原变更回执，另按报告参数与本批更正后的完整供数当次返回
        ``recheck`` 复核结果；原报告的固化明细不受影响。
    """
    conn = db.get_conn()
    with db.lock():
        with db.transaction(conn):
            if batch.report_id is not None:
                try:
                    reports.require_report_params(conn, batch.report_id)
                except reports.ReportNotFound:
                    raise HTTPException(
                        status_code=404,
                        detail={
                            "message": "报告号无效，整批拒绝，未写入任何校时点",
                            "report_id": batch.report_id,
                        },
                    )
            try:
                ids = core.upsert_calibrations(
                    conn, [c.model_dump() for c in batch.calibrations]
                )
            except core.MonotonicViolation as exc:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "校时点使段内 UTC 倒退，整批拒绝，旧数据照常供数",
                        "violations": exc.violations,
                    },
                )
            changed = core.fetch_states(conn, ids)
            recheck = (
                reports.recheck(conn, batch.report_id)
                if batch.report_id is not None
                else None
            )
    return CalibrationBatchResult(
        changed_count=len(changed),
        changed=changed,
        report_id=batch.report_id,
        recheck=recheck,
    )


# ---------------------------------------------------------------- 查询

@app.get("/tide-levels", response_model=TideQueryResult, summary="按 UTC 范围查可用潮位")
def tide_levels(
    start_ms: int = Query(ge=0, description="起始 UTC 毫秒（含）"),
    end_ms: int = Query(ge=0, description="结束 UTC 毫秒（含）"),
) -> TideQueryResult:
    """只返回已校时且无冲突（status=ok）的观测，按 UTC 毫秒升序。"""
    if end_ms < start_ms:
        raise HTTPException(status_code=422, detail="end_ms 不得小于 start_ms")
    conn = db.get_conn()
    with db.lock():
        rows = conn.execute(
            "SELECT obs_id, utc_ms, tide_level FROM observations "
            "WHERE status = ? AND utc_ms BETWEEN ? AND ? "
            "AND tide_level = tide_level "
            "ORDER BY utc_ms, obs_id",
            (core.OK, start_ms, end_ms),
        ).fetchall()
    # NaN 不自相等已在 SQL 剔除；Inf/-Inf 在应用层按“有限可供数”再过滤
    items = [
        TidePoint(obs_id=r["obs_id"], utc_ms=r["utc_ms"], tide_level=r["tide_level"])
        for r in rows
        if math.isfinite(r["tide_level"])
    ]
    return TideQueryResult(count=len(items), items=items)


@app.get(
    "/observations/{obs_id}",
    response_model=ObservationDetail,
    summary="按观测 ID 查状态和原因",
)
def get_observation(obs_id: str) -> ObservationDetail:
    conn = db.get_conn()
    with db.lock():
        row = conn.execute(
            "SELECT * FROM observations WHERE obs_id = ?", (obs_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"观测 '{obs_id}' 不存在")
    calibration = None
    if row["cal_lo_counter"] is not None:
        calibration = CalibrationBasis(
            lo=CalibrationEndpoint(
                counter=row["cal_lo_counter"], utc_ms=row["cal_lo_utc_ms"]
            ),
            hi=CalibrationEndpoint(
                counter=row["cal_hi_counter"], utc_ms=row["cal_hi_utc_ms"]
            ),
        )
    return ObservationDetail(
        obs_id=row["obs_id"],
        boot_segment=row["boot_segment"],
        counter=row["counter"],
        tide_level=row["tide_level"],
        utc_ms=row["utc_ms"],
        status=row["status"],
        reason=row["reason"],
        calibration=calibration,
    )


# ---------------------------------------------------------------- 浸水报告

@app.post(
    "/inundation-reports",
    response_model=InundationReport,
    status_code=201,
    summary="签发连续浸水报告",
)
def issue_inundation_report(req: InundationReportRequest) -> InundationReport:
    """从同一已提交状态取扩展窗内（含两端）有限的可供数潮位做核查并签发报告。

    合法连续曲线受最大升降速率限制，且在各实测时刻须落在读数误差内。
    返回**必然**（所有合法曲线）与**可能**（至少一条合法曲线）的 UTC 毫秒
    起点区间（可带小数），分别要求曲线从起点持续达到或超过门槛满最短浸水
    时长，且全程在核查窗内。

    - 扩展窗无可用读数：必然为空，可能为窗内留足时长的全部起点；
    - 读数矛盾：明细中只报一对矛盾观测 ID，两类起点区间均为空；
    - 签发成功即返回报告号并固定参数与明细，之后可按号复算原报告；
      签发失败不留任何报告。
    """
    params = req.model_dump()
    conn = db.get_conn()
    with db.lock():
        with db.transaction(conn):
            report = reports.issue_report(conn, params)
    return InundationReport(**report)


@app.get(
    "/inundation-reports",
    response_model=list[ReportSummary],
    summary="连续浸水报告清单",
)
def list_inundation_reports() -> list[ReportSummary]:
    conn = db.get_conn()
    with db.lock():
        rows = reports.list_reports(conn)
    return [ReportSummary(**r) for r in rows]


@app.get(
    "/inundation-reports/{report_id}",
    response_model=InundationReport,
    summary="按报告号取回原报告（更正或重启后仍可复算）",
)
def get_inundation_report(report_id: str) -> InundationReport:
    conn = db.get_conn()
    with db.lock():
        report = reports.load_report(conn, report_id)
    if report is None:
        raise HTTPException(
            status_code=404,
            detail={"message": "报告号无效", "report_id": report_id},
        )
    return InundationReport(**report)


@app.get("/health", summary="健康检查")
def health() -> dict:
    return {"status": "up"}

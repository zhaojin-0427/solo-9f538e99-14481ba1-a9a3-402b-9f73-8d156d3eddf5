"""端到端测试：连续浸水报告的签发、固定快照、按号复算与整批拒绝。"""

import os

import pytest
from fastapi.testclient import TestClient

from app import db, reports
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def clean_tables():
    conn = db.get_conn()
    with db.lock():
        conn.execute("DELETE FROM observations")
        conn.execute("DELETE FROM calibration_points")
        conn.execute("DELETE FROM inundation_reports")
    yield


# ---------------------------------------------------------------- 工具

def post_obs(items, expect=200):
    r = client.post("/observations", json={"observations": items})
    assert r.status_code == expect, r.text
    return r.json()


def post_cal(items, report_id=None, expect=200):
    body = {"calibrations": items}
    if report_id is not None:
        body["report_id"] = report_id
    r = client.post("/calibrations", json=body)
    assert r.status_code == expect, r.text
    return r.json()


def seed_segment(segment, cals, obs):
    post_cal(cals)
    post_obs(obs)


REPORT_PARAMS = {
    "window_start_ms": 10000,
    "window_end_ms": 14000,
    "padding_ms": 500,
    "threshold": 1.0,
    "reading_error": 0.05,
    "max_rate": 1.0,
    "min_duration_ms": 1000,
}


def issue(params=None, expect=201):
    r = client.post("/inundation-reports", json=params or REPORT_PARAMS)
    assert r.status_code == expect, r.text
    return r.json()


# ---------------------------------------------------------------- 签发基础

def test_issue_report_returns_id_and_fixed_params():
    seed_segment(
        "A",
        [{"boot_segment": "A", "counter": 0, "utc_ms": 9000},
         {"boot_segment": "A", "counter": 6000, "utc_ms": 15000}],
        [{"obs_id": "a", "boot_segment": "A", "counter": 2000, "tide_level": 2.0}],
    )
    body = issue()
    assert body["report_id"].startswith("R-")
    assert body["params"] == REPORT_PARAMS
    assert body["detail"]["extended_window"] == {"start_ms": 9500, "end_ms": 14500}
    assert body["detail"]["reading_count"] == 1
    assert body["detail"]["readings"][0]["obs_id"] == "a"
    assert body["detail"]["contradiction"] is None
    assert body["created_at"]

    # 清单与按号取回一致
    listing = client.get("/inundation-reports").json()
    assert [x["report_id"] for x in listing] == [body["report_id"]]
    got = client.get(f"/inundation-reports/{body['report_id']}").json()
    assert got == body


def test_report_404():
    r = client.get("/inundation-reports/R-nope")
    assert r.status_code == 404
    assert r.json()["detail"]["report_id"] == "R-nope"


def test_request_validation_rules():
    # 窗倒置
    r = client.post("/inundation-reports", json={**REPORT_PARAMS,
                                                 "window_end_ms": 1})
    assert r.status_code == 422
    # 负误差 / 非正速率 / 非正时长 / 非有限门槛（字符串形式，绕过客户端编码限制）
    for patch in (
        {"reading_error": -0.1},
        {"max_rate": 0.0},
        {"min_duration_ms": 0},
        {"threshold": "NaN"},
        {"threshold": "Infinity"},
    ):
        r = client.post("/inundation-reports", json={**REPORT_PARAMS, **patch})
        assert r.status_code == 422, patch
    # 签发失败不留报告
    assert client.get("/inundation-reports").json() == []


# ---------------------------------------------------------------- 取数范围与有限性

def test_extended_window_inclusive_and_nonfinite_excluded():
    seed_segment(
        "A",
        [{"boot_segment": "A", "counter": 0, "utc_ms": 0},
         {"boot_segment": "A", "counter": 20000, "utc_ms": 20000}],
        [
            {"obs_id": "left_edge", "boot_segment": "A", "counter": 9500, "tide_level": 2.0},
            {"obs_id": "inside", "boot_segment": "A", "counter": 12000, "tide_level": 2.0},
            {"obs_id": "right_edge", "boot_segment": "A", "counter": 14500, "tide_level": 2.0},
            {"obs_id": "outside", "boot_segment": "A", "counter": 9499, "tide_level": 2.0},
            {"obs_id": "inf", "boot_segment": "A", "counter": 12100, "tide_level": "Infinity"},
            {"obs_id": "nan", "boot_segment": "A", "counter": 12200, "tide_level": "NaN"},
        ],
    )
    body = issue()
    ids = {r["obs_id"] for r in body["detail"]["readings"]}
    assert ids == {"left_edge", "inside", "right_edge"}  # 含两端，排除窗外与非有限
    # /tide-levels 同样剔除非有限
    tide = client.get("/tide-levels", params={"start_ms": 0, "end_ms": 20000}).json()
    assert "inf" not in {x["obs_id"] for x in tide["items"]}
    assert "nan" not in {x["obs_id"] for x in tide["items"]}
    # 非有限原文仍可按 ID 查回；为输出合规 JSON，非有限值序列化为 null
    detail = client.get("/observations/inf").json()
    assert detail["tide_level"] is None and detail["status"] == "ok"
    assert client.get("/observations/nan").json()["tide_level"] is None


def test_conflict_and_pending_observations_not_served():
    post_cal([
        {"boot_segment": "A", "counter": 0, "utc_ms": 10000},
        {"boot_segment": "B", "counter": 0, "utc_ms": 10000},
    ])
    post_obs([
        {"obs_id": "cf1", "boot_segment": "A", "counter": 0, "tide_level": 2.0},
        {"obs_id": "cf2", "boot_segment": "B", "counter": 0, "tide_level": 2.0},
        {"obs_id": "pend", "boot_segment": "A", "counter": 99999, "tide_level": 2.0},
    ])
    body = issue()
    assert body["detail"]["readings"] == []
    # 无可用读数：必然为空，可能为窗内留足时长的起点 [10000, 13000]
    assert body["detail"]["necessary_start_intervals"] == []
    assert body["detail"]["possible_start_intervals"] == [
        {"start": 10000.0, "end": 13000.0}
    ]


def test_window_too_narrows_yields_empty_possible():
    seed_segment(
        "A",
        [{"boot_segment": "A", "counter": 0, "utc_ms": 0},
         {"boot_segment": "A", "counter": 100, "utc_ms": 20000}],
        [{"obs_id": "a", "boot_segment": "A", "counter": 50, "tide_level": 2.0}],
    )
    body = issue({**REPORT_PARAMS, "min_duration_ms": 4001})
    assert body["detail"]["possible_start_intervals"] == []
    assert body["detail"]["necessary_start_intervals"] == []


# ---------------------------------------------------------------- 矛盾

def test_contradiction_reports_one_pair_and_empty_intervals():
    seed_segment(
        "A",
        [{"boot_segment": "A", "counter": 0, "utc_ms": 10000},
         {"boot_segment": "A", "counter": 2000, "utc_ms": 12000}],
        [  # 同刻潮位差 0.5 > 2ε=0.1
            {"obs_id": "x", "boot_segment": "A", "counter": 500, "tide_level": 2.0},
            {"obs_id": "y", "boot_segment": "A", "counter": 500, "tide_level": 2.5},
        ],
    )
    body = issue()
    assert body["detail"]["contradiction"] == {"obs_id_a": "x", "obs_id_b": "y"}
    assert body["detail"]["possible_start_intervals"] == []
    assert body["detail"]["necessary_start_intervals"] == []
    # 矛盾不视为失败：报告已签发并固定，按号取回仍是同一结果
    rid = body["report_id"]
    assert client.get(f"/inundation-reports/{rid}").json()["detail"] == body["detail"]


def test_contradiction_pair_is_deterministic_and_single():
    seed_segment(
        "A",
        [{"boot_segment": "A", "counter": 0, "utc_ms": 10000},
         {"boot_segment": "A", "counter": 4000, "utc_ms": 14000}],
        [
            {"obs_id": "p", "boot_segment": "A", "counter": 100, "tide_level": 5.0},
            {"obs_id": "q", "boot_segment": "A", "counter": 1100, "tide_level": 0.0},
            {"obs_id": "r", "boot_segment": "A", "counter": 2100, "tide_level": 5.0},
        ],
    )
    body = issue()
    assert body["detail"]["contradiction"] == {"obs_id_a": "p", "obs_id_b": "q"}


# ---------------------------------------------------------------- 必然/可能内容

def test_necessary_and_possible_intervals_content():
    # 单点 2.0@12000，ε=.05，h=1，r=1m/s：
    # 可能全域 [10000,13000]；L>=1 集合 [11050,12950]，起点右端再减 1000
    seed_segment(
        "A",
        [{"boot_segment": "A", "counter": 0, "utc_ms": 0},
         {"boot_segment": "A", "counter": 20000, "utc_ms": 20000}],
        [{"obs_id": "a", "boot_segment": "A", "counter": 12000, "tide_level": 2.0}],
    )
    body = issue()
    assert body["detail"]["possible_start_intervals"] == [
        {"start": 10000.0, "end": 13000.0}
    ]
    assert body["detail"]["necessary_start_intervals"] == [
        {"start": 11050.0, "end": 11950.0}
    ]


def test_decimal_interval_bounds_preserved():
    # 非整除参数产生小数边界：r=0.8 m/s -> r/ms=0.0008
    seed_segment(
        "A",
        [{"boot_segment": "A", "counter": 0, "utc_ms": 0},
         {"boot_segment": "A", "counter": 20000, "utc_ms": 20000}],
        [{"obs_id": "a", "boot_segment": "A", "counter": 12000, "tide_level": 2.0}],
    )
    params = {**REPORT_PARAMS, "max_rate": 0.8, "reading_error": 0.03}
    body = issue(params)
    iv = body["detail"]["necessary_start_intervals"][0]
    # δ=(2-.03-1)/0.0008=1212.5；L>=1 集合 [10787.5,13212.5]
    assert iv["start"] == 10787.5
    assert iv["end"] == 12212.5  # 13212.5 - 1000


# ---------------------------------------------------------------- 复算与固定快照

def test_report_snapshot_immutable_after_correction():
    seed_segment(
        "A",
        [{"boot_segment": "A", "counter": 0, "utc_ms": 10000},
         {"boot_segment": "A", "counter": 4000, "utc_ms": 14000}],
        [{"obs_id": "a", "boot_segment": "A", "counter": 2000, "tide_level": 2.0}],
    )
    first = issue()
    rid = first["report_id"]

    # 更正校时点，让观测时刻平移：原报告明细不变
    post_cal([{"boot_segment": "A", "counter": 4000, "utc_ms": 18000}])
    again = client.get(f"/inundation-reports/{rid}").json()
    assert again == first
    assert again["detail"]["readings"][0]["utc_ms"] == 12000  # 签发时的旧时刻


def test_recheck_with_invalid_report_id_rejects_whole_batch():
    post_cal([{"boot_segment": "A", "counter": 0, "utc_ms": 10000}])
    resp = post_cal(
        [{"boot_segment": "A", "counter": 1000, "utc_ms": 11000}],
        report_id="R-ghost",
        expect=404,
    )
    assert "整批拒绝" in resp["detail"]["message"]
    # 校时点未落库：超区间观测仍待校时
    post_obs([{"obs_id": "a", "boot_segment": "A", "counter": 1000, "tide_level": 2.0}])
    assert client.get("/observations/a").json()["status"] == "pending_calibration"
    # 没有产生任何报告
    assert client.get("/inundation-reports").json() == []


def test_recheck_keeps_receipt_and_returns_fresh_result():
    # 初始校时：观测 a@12000 为高点
    seed_segment(
        "A",
        [{"boot_segment": "A", "counter": 0, "utc_ms": 10000},
         {"boot_segment": "A", "counter": 4000, "utc_ms": 14000}],
        [{"obs_id": "a", "boot_segment": "A", "counter": 2000, "tide_level": 2.0}],
    )
    rid = issue()["report_id"]

    # 本批更正使 a 平移到 13000；回执仍为 changed 结构，另附 recheck
    resp = post_cal(
        [{"boot_segment": "A", "counter": 4000, "utc_ms": 16000}],
        report_id=rid,
    )
    assert resp["report_id"] == rid
    assert {c["obs_id"] for c in resp["changed"]} == {"a"}
    assert resp["changed"][0]["utc_ms"] == 13000
    assert resp["recheck"]["readings"][0]["utc_ms"] == 13000
    assert resp["recheck"]["extended_window"] == {"start_ms": 9500, "end_ms": 14500}

    # 原报告固定不变
    fixed = client.get(f"/inundation-reports/{rid}").json()
    assert fixed["detail"]["readings"][0]["utc_ms"] == 12000


def test_recheck_contradiction_still_commits_calibrations():
    seed_segment(
        "A",
        [{"boot_segment": "A", "counter": 0, "utc_ms": 10000},
         {"boot_segment": "A", "counter": 4000, "utc_ms": 14000}],
        [
            {"obs_id": "a", "boot_segment": "A", "counter": 1000, "tide_level": 2.0},
            {"obs_id": "b", "boot_segment": "A", "counter": 1000, "tide_level": 2.5},
        ],
    )
    rid = issue()["report_id"]  # 此刻同刻差 0.5 > 0.1，已矛盾
    # 附报告号再提交合法校时点：提交成功，recheck 仍报矛盾
    resp = post_cal(
        [{"boot_segment": "A", "counter": 4000, "utc_ms": 14500}],
        report_id=rid,
    )
    assert resp["recheck"]["contradiction"] == {"obs_id_a": "a", "obs_id_b": "b"}
    # 校时点确实已提交（校时依据上界换成新值）
    basis = client.get("/observations/a").json()["calibration"]
    assert basis["hi"]["utc_ms"] == 14500


def test_recheck_uses_complete_current_supply_including_late_observations():
    seed_segment(
        "A",
        [{"boot_segment": "A", "counter": 0, "utc_ms": 10000},
         {"boot_segment": "A", "counter": 4000, "utc_ms": 14000}],
        [{"obs_id": "a", "boot_segment": "A", "counter": 2000, "tide_level": 2.0}],
    )
    rid = issue()["report_id"]
    assert client.get(f"/inundation-reports/{rid}").json()["detail"]["reading_count"] == 1

    # 签发后新增观测（未附报告的普通提交），复算须纳入完整供数
    post_obs([{"obs_id": "b", "boot_segment": "A", "counter": 3000, "tide_level": 2.1}])
    resp = post_cal(
        [{"boot_segment": "A", "counter": 4000, "utc_ms": 14000}],  # 同值替换，合法
        report_id=rid,
    )
    assert {r["obs_id"] for r in resp["recheck"]["readings"]} == {"a", "b"}


# ---------------------------------------------------------------- 重启持久化

def test_legacy_database_migrates_without_data_loss(tmp_path):
    import sqlite3

    legacy = tmp_path / "legacy.db"
    raw = sqlite3.connect(legacy)
    # 旧版本库：无 inundation_reports，tide_level 仍为 NOT NULL，user_version=0
    raw.executescript(
        """
        CREATE TABLE calibration_points (
            boot_segment TEXT NOT NULL, counter INTEGER NOT NULL,
            utc_ms INTEGER NOT NULL, PRIMARY KEY (boot_segment, counter));
        CREATE TABLE observations (
            obs_id TEXT PRIMARY KEY, boot_segment TEXT NOT NULL,
            counter INTEGER NOT NULL, tide_level REAL NOT NULL,
            utc_ms INTEGER, status TEXT NOT NULL, reason TEXT,
            cal_lo_counter INTEGER, cal_lo_utc_ms INTEGER,
            cal_hi_counter INTEGER, cal_hi_utc_ms INTEGER);
        INSERT INTO calibration_points VALUES ('A', 0, 10000), ('A', 4000, 14000);
        INSERT INTO observations VALUES
            ('a', 'A', 2000, 2.0, 12000, 'ok', NULL, 0, 10000, 4000, 14000);
        """
    )
    raw.commit()
    raw.close()

    os.environ["BUOY_DB"] = str(legacy)
    db.reset_for_tests()
    try:
        c = TestClient(app)
        # 旧数据完好
        assert c.get("/observations/a").json()["utc_ms"] == 12000
        # 新表可用：可签发并取回报告
        rid = c.post("/inundation-reports", json=REPORT_PARAMS).json()["report_id"]
        assert c.get(f"/inundation-reports/{rid}").status_code == 200
        ver = db.get_conn().execute("PRAGMA user_version").fetchone()[0]
        assert ver >= 1
    finally:
        db.reset_for_tests()
        os.environ["BUOY_DB"] = ":memory:"
        db.get_conn()


def test_report_survives_process_restart(tmp_path):
    db_path = tmp_path / "restart.db"
    os.environ["BUOY_DB"] = str(db_path)
    db.reset_for_tests()
    try:
        c1 = TestClient(app)
        c1.post("/calibrations", json={"calibrations": [
            {"boot_segment": "A", "counter": 0, "utc_ms": 10000},
            {"boot_segment": "A", "counter": 4000, "utc_ms": 14000},
        ]})
        c1.post("/observations", json={"observations": [
            {"obs_id": "a", "boot_segment": "A", "counter": 2000, "tide_level": 2.0},
        ]})
        created = c1.post("/inundation-reports", json=REPORT_PARAMS).json()

        # 模拟重启：丢弃进程内连接后重新建连
        db.reset_for_tests()
        c2 = TestClient(app)
        fetched = c2.get(f"/inundation-reports/{created['report_id']}").json()
        assert fetched == created
        # 重启后仍可附号复算
        r = c2.post("/calibrations", json={
            "calibrations": [{"boot_segment": "A", "counter": 4000, "utc_ms": 14000}],
            "report_id": created["report_id"],
        })
        assert r.status_code == 200
        assert r.json()["recheck"]["reading_count"] == 1
    finally:
        db.reset_for_tests()
        os.environ["BUOY_DB"] = ":memory:"
        db.get_conn()  # 重建内存库，后续测试回到隔离环境


# ---------------------------------------------------------------- 文档

def test_docs_list_new_endpoints():
    paths = client.get("/openapi.json").json()["paths"]
    for p in ("/inundation-reports", "/inundation-reports/{report_id}"):
        assert p in paths

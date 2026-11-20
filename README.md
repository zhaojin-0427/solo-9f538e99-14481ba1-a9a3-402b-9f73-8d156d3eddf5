# 单浮标潮位校时与连续浸水核查服务

浮标每次重启都会把毫秒计数归零，潮位分析却需要一条可信的 UTC 时间线。
本服务（FastAPI + SQLite）接收「开机段 + 段内计数」的观测与校时点，
把计数线性换算成 UTC 毫秒，维护观测的可用状态供分析查询，并可在
`/docs` 签发**连续浸水报告**：报告参数与明细固定留档，校时更正后
仍可按报告号复算复核。

## 快速开始

```bash
docker compose up --build
# 服务监听 http://localhost:8000 ，交互式文档在 http://localhost:8000/docs
```

本地开发（Python 3.11+）：

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest                 # 运行测试
```

数据默认存放在命名卷 `buoy-data` 的 `/data/buoy.db`（可用环境变量
`BUOY_DB` 覆盖）。SQLite 为单写者模型，容器以单进程运行。

## 概念与规则

- **开机段（boot_segment）**：一次开机周期。计数只在段内单调，重启归零。
- **校时点（calibration point）**：`(开机段, 计数) -> UTC 毫秒`。
  同段同计数重复提交时新值替换旧值。
- **观测（observation）**：`(观测 ID, 开机段, 计数, 潮位)`。
  同一观测 ID 重复提交会覆盖原文并重算。

### 校时换算

- 计数落在相邻两个校时点之间时线性换算并**向下取整**：
  `utc = t_lo + (c - c_lo) * (t_hi - t_lo) // (c_hi - c_lo)`。
- 恰好命中校时点时直接取该点 UTC；只有一个校时点时仅该计数可换算。
- 计数落在本段校时区间之外：保留原文，状态置 `pending_calibration`（待校时）。

### 观测状态机

| 状态 | 含义 | 是否供分析 |
|---|---|---|
| `ok` | 已校时且无冲突 | ✅ |
| `pending_calibration` | 计数在校时区间外，待校时 | ❌ |
| `conflict` | 不同开机段落在同一 UTC 毫秒 | ❌ |

- **补传/更正校时点**会重算受影响观测（区间扩大可让待校时观测转为可用）。
- **冲突**：同一 UTC 毫秒被 ≥2 个不同开机段的观测占用时，这些观测全部
  置为 `conflict` 不供分析；更正校时点或覆盖观测解除冲突后自动恢复。
  同一开机段内多条观测共用同一毫秒不算冲突。
- **整批拒绝**：校时点合并后若使任一段内 UTC 随计数倒退，整个校时批次
  以 `409` 拒绝并回滚，旧数据照常供数。

### 原子性

每批提交（原文 + 校时点 + 重算 + 冲突刷新）都在**单个 SQLite 事务**中
完成，并由进程级锁串行化读写：原文、校时依据、分析供数同次生效，
查询和（崩溃后的）重启都不会看到半次重算的中间态。

## 连续浸水报告

闸口巡检在 `/docs` 签发连续浸水报告，供校时更正后复核。提交核查窗的
UTC 毫秒起止、向两侧各扩的毫秒数、门槛潮位（米）、读数误差（非负，米）、
最大升降速率（正数，米/秒）和最短浸水时长（正数，毫秒）。

### 取数

从**同一已提交状态**取扩展窗 `[start-padding, end+padding]`（含两端）内
**有限的可供数潮位**（`status=ok` 且潮位为有限实数；非有限值、冲突与
待校时观测均不供数），按 `(UTC 毫秒, 观测 ID)` 升序参与核查。

### 合法曲线与判定

合法连续曲线（时间以毫秒计）：

- 受速率限制：任意两时刻 `|f(t)-f(s)| ≤ r|t-s|`，`r = max_rate/1000`
  （米/毫秒）；
- 在每个实测时刻 `t_i` 落在读数误差内：`|f(t_i) - y_i| ≤ ε`。

由全部读数可推出任意时刻的可达上、下包络（二者本身都是合法曲线）：

```
U(t) = min_i (y_i + ε + r|t - t_i|)
L(t) = max_i (y_i - ε - r|t - t_i|)
```

起点 `s` 须在核查窗内并留足时长（`s ∈ [start, end - duration]`），
浸水段 `[s, s+duration]` 全程在核查窗内：

- **可能**起点：至少一条合法曲线在整段达到或超过门槛
  ⇔ 浸水段内 `U(t) ≥ h` 处处成立；
- **必然**起点：所有合法曲线在整段达到或超过门槛
  ⇔ 浸水段内 `L(t) ≥ h` 处处成立。

区间端点可带小数，服务统一保留 6 位小数输出。

- 扩展窗**无可用读数**时：必然为空，可能为窗内留足时长的全部起点。
- **读数矛盾**：两条读数无法同时满足
  `|y_i-y_j| ≤ 2ε + r|t_i-t_j|` 即不存在任何合法曲线；明细中只报
  **一对**矛盾观测 ID（时间序最靠前的一对），两类起点区间均为空。
  矛盾不视为签发失败，报告照常留档。

### 签发、固定与复算

- `POST /inundation-reports` 签发成功即返回**报告号**，参数与完整明细
  （扩展窗、读数快照、矛盾、必然/可能区间）整体固化；之后的观测或
  校时更正不影响原报告，进程重启后按号取回的仍是同一份报告。
  签发失败（如参数非法、事务回滚）不留任何报告。
- `POST /calibrations` 可附 `report_id`：
  - 报告号无效 → **整批拒绝**（404），不写入任何校时点、不产生报告；
  - 合法校时点照常提交，即使复算发现读数矛盾也不回滚；
  - 响应保留原变更回执，另按**报告参数**与本批更正后的**完整供数**
    当次返回 `recheck` 核查结果；原报告的固化明细不变。

## API

均可在 `/docs` 中直接调用。

### `POST /observations` — 分批提交观测

```json
{
  "observations": [
    {"obs_id": "obs-1", "boot_segment": "boot-1", "counter": 1500, "tide_level": 2.34}
  ]
}
```

### `POST /calibrations` — 分批提交校时点（可附报告号复核）

```json
{
  "calibrations": [
    {"boot_segment": "boot-1", "counter": 1000, "utc_ms": 1700000000000},
    {"boot_segment": "boot-1", "counter": 2000, "utc_ms": 1700000001000}
  ],
  "report_id": "R-0123456789abcdef"
}
```

`report_id` 可省略。给出时报告号无效则整批拒绝（404）；合法时校时点
照常提交，响应在原回执外附加 `recheck`（按报告参数与更正后完整供数的
当次复核结果）。

两个提交接口的响应都会列出**本次提交中时刻或状态发生变化的观测 ID**
（含新观测，以及被卷入/解除冲突的其他观测）：

```json
{
  "changed_count": 1,
  "changed": [{"obs_id": "obs-1", "utc_ms": 1700000000500, "status": "ok"}],
  "report_id": null,
  "recheck": null
}
```

### `POST /inundation-reports` — 签发连续浸水报告

```json
{
  "window_start_ms": 1700000000000,
  "window_end_ms": 1700003600000,
  "padding_ms": 60000,
  "threshold": 2.5,
  "reading_error": 0.05,
  "max_rate": 0.8,
  "min_duration_ms": 1800000
}
```

成功返回 `201` 与报告号，参数与明细固定留档：

```json
{
  "report_id": "R-0123456789abcdef",
  "created_at": "2026-11-20T07:49:15.979469+00:00",
  "params": { "...": "原样回显的七个参数" },
  "detail": {
    "extended_window": {"start_ms": 1699999940000, "end_ms": 1700003660000},
    "reading_count": 3,
    "readings": [
      {"obs_id": "obs-1", "utc_ms": 1700000000500, "tide_level": 2.34}
    ],
    "contradiction": null,
    "possible_start_intervals": [{"start": 1700000000000.0, "end": 1700001800000.0}],
    "necessary_start_intervals": []
  }
}
```

读数矛盾时 `contradiction` 为 `{"obs_id_a": "...", "obs_id_b": "..."}`，
两个起点区间均为空。

### `GET /inundation-reports` / `GET /inundation-reports/{report_id}`

报告清单（报告号、签发时间、核查窗）与按号取回完整原报告；
报告号无效返回 `404`。

### 其他

### `GET /tide-levels?start_ms=...&end_ms=...` — 按 UTC 范围查可用潮位

只返回 `status=ok` 的观测，区间含端点，按 UTC 毫秒升序。

### `GET /observations/{obs_id}` — 按观测 ID 查状态和原因

返回原文、换算结果、状态、原因与校时依据（用于换算的上下界校时点）：

```json
{
  "obs_id": "obs-1", "boot_segment": "boot-1", "counter": 1500,
  "tide_level": 2.34, "utc_ms": 1700000000500, "status": "ok",
  "reason": null,
  "calibration": {
    "lo": {"counter": 1000, "utc_ms": 1700000000000},
    "hi": {"counter": 2000, "utc_ms": 1700000001000}
  }
}
```

### 其他

- `GET /health` — 健康检查
- `GET /docs` — Swagger UI；`GET /openapi.json` — OpenAPI 模式

## 目录结构

```
app/
  main.py         # FastAPI 路由与请求/响应模型
  core.py         # 校时换算、批量写入、重算、冲突检测、变更快照
  inundation.py   # 连续浸水核查的纯数学分析（包络、矛盾、必然/可能区间）
  reports.py      # 报告签发、固定快照、按号取回与复算
  db.py           # SQLite 连接、模式、事务与锁
tests/
  test_api.py
  test_inundation.py
Dockerfile
docker-compose.yml
```

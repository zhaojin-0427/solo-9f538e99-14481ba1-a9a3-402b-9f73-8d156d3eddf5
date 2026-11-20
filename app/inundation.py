"""连续浸水核查的纯数学分析。

输入一组已供数潮位读数（有限值，按 UTC 毫秒排序）与核查参数，求：

- 读数是否矛盾（不存在任何合法连续曲线）；
- **可能**浸水起点区间：至少存在一条合法曲线，从起点持续达到或超过门槛
  满最短浸水时长，且全程在核查窗内；
- **必然**浸水起点区间：所有合法曲线都满足上述浸水条件。

合法曲线 f（时间以毫秒计）：

  1.  Lipschitz 速率限制：|f(t) - f(s)| <= r * |t - s|，
     其中 r = max_rate（米/秒）/ 1000，单位米/毫秒；
  2.  在每个实测时刻 t_i：|f(t_i) - y_i| <= reading_error（ε）。

可达包络（二者本身均为合法曲线）：

  U(t) = min_i (y_i + ε + r|t - t_i|)   # 任意合法曲线 f 恒有 f(t) <= U(t)
  L(t) = max_i (y_i - ε - r|t - t_i|)   # 任意合法曲线 f 恒有 f(t) >= L(t)

无读数时不存在约束，任一点都可作为某条合法曲线的取值：
  U ≡ +∞，L ≡ -∞。

因此：

- 存在合法曲线满足浸水 [s, s+d] ⇔ min_{t∈[s,s+d]} U(t) >= h（可能）；
- 所有合法曲线都满足   ⇔ min_{t∈[s,s+d]} L(t) >= h（必然）。

起点 s 本身还须落在核查窗 [W_lo, W_hi] 且留足时长：s ∈ [W_lo, W_hi - d]。
"""

from __future__ import annotations

import math

# 浮点比较容差（按输入量级），消除同一数值的不同运算路径误差
_TOL = 1e-12


def _round6(x: float) -> float:
    """输出统一保留 6 位小数，并归一化负零。"""
    v = round(float(x), 6)
    return v + 0.0


def _eps(scale: float) -> float:
    return _TOL * max(1.0, abs(scale))


def find_contradiction(
    readings: list[dict], reading_error: float, rate_per_ms: float
) -> dict | None:
    """两两一致性检查：返回一对矛盾观测 ID，无矛盾返回 None。

    两条观测可被同一条合法曲线同时满足，当且仅当
        |y_i - y_j| <= 2ε + r * |t_i - t_j|。
    读数已按 (utc_ms, obs_id) 排序，故只报时间序最靠前的一对
    （同一时刻则按观测 ID 字典序）。
    """
    n = len(readings)
    for i in range(n):
        a = readings[i]
        for j in range(i + 1, n):
            b = readings[j]
            dt = b["utc_ms"] - a["utc_ms"]  # 排序保证 >= 0
            limit = 2.0 * reading_error + rate_per_ms * dt
            if abs(b["tide_level"] - a["tide_level"]) > limit + _eps(limit):
                return {"obs_id_a": a["obs_id"], "obs_id_b": b["obs_id"]}
    return None


def _possible_components(
    readings: list[dict], rate_per_ms: float, reading_error: float, threshold: float
) -> list[tuple[float, float]]:
    """{t : U(t) >= h} 的连通分量（闭区间）。

    每条读数在 y_i + ε < h 时排除一个开区间
        (t_i - d, t_i + d)，d = (h - y_i - ε) / r；
    y_i + ε >= h 的读数其约束永远不会让 U(t) < h，不产生排除。
    补集（闭集）即上包络达到门槛的时刻集合。
    仅正长度重叠才合并：仅端点相触时该点仍可行，须保留在补集中。
    """
    excluded: list[tuple[float, float]] = []
    for rd in readings:
        t = rd["utc_ms"]
        y = rd["tide_level"]
        d = (threshold - y - reading_error) / rate_per_ms
        if d <= 0:
            continue
        excluded.append((t - d, t + d))
    excluded.sort()

    merged: list[tuple[float, float]] = []
    for lo, hi in excluded:
        if merged and lo < merged[-1][1]:
            if hi > merged[-1][1]:
                merged[-1] = (merged[-1][0], hi)
        else:
            merged.append((lo, hi))

    # 实数轴上的补集（闭区间，含 ±∞ 哨兵）
    complement: list[tuple[float, float]] = []
    cursor = -math.inf
    for lo, hi in merged:
        if lo > cursor:
            complement.append((cursor, lo))
        if hi > cursor:
            cursor = hi
    complement.append((cursor, math.inf))
    return complement


def _necessary_components(
    readings: list[dict], rate_per_ms: float, reading_error: float, threshold: float
) -> list[tuple[float, float]]:
    """{t : L(t) >= h} 的连通分量（闭区间并集）。

    L(t) = max_i (y_i - ε - r|t - t_i|)，故 L(t) >= h 当且仅当**存在**读数 i
    使 |t - t_i| <= δ_i，δ_i = (y_i - ε - h) / r；即各读数闭区间
        [t_i - δ_i, t_i + δ_i]
    的**并集**（δ_i < 0 的读数不贡献区间）。无读数时为空。
    """
    intervals: list[tuple[float, float]] = []
    for rd in readings:
        delta = (rd["tide_level"] - reading_error - threshold) / rate_per_ms
        if delta < 0:
            continue
        intervals.append((rd["utc_ms"] - delta, rd["utc_ms"] + delta))
    intervals.sort()

    merged: list[tuple[float, float]] = []
    for lo, hi in intervals:
        if merged and lo <= merged[-1][1]:  # 闭区间端点相触即连通
            if hi > merged[-1][1]:
                merged[-1] = (merged[-1][0], hi)
        else:
            merged.append((lo, hi))
    return merged


def _emit(intervals: list[tuple[float, float]]) -> list[dict]:
    return [{"start": _round6(lo), "end": _round6(hi)} for lo, hi in intervals]


def analyze(
    *,
    window_start_ms: int,
    window_end_ms: int,
    padding_ms: int,
    threshold: float,
    reading_error: float,
    max_rate: float,
    min_duration_ms: int,
    readings: list[dict],
) -> dict:
    """执行一次完整核查，返回可直接序列化的结果明细。"""
    rate_per_ms = max_rate / 1000.0
    extended_start = window_start_ms - padding_ms
    extended_end = window_end_ms + padding_ms

    contradiction = find_contradiction(readings, reading_error, rate_per_ms)

    possible: list[tuple[float, float]] = []
    necessary: list[tuple[float, float]] = []

    if contradiction is None:
        # 合法起点域：核查窗内且能留足最短浸水时长
        s_lo = float(window_start_ms)
        s_hi = float(window_end_ms - min_duration_ms)

        if s_lo <= s_hi:
            duration = float(min_duration_ms)

            # 可能：上包络全程 >= 门槛的时间分量 C 上，起点 s 还须
            # 让整个浸水段 [s, s+d] 落在 C 内，即 s <= C_hi - d；
            # 左端 s >= C_lo 即可（s_lo 已保证在核查窗内）。
            for comp_lo, comp_hi in _possible_components(
                readings, rate_per_ms, reading_error, threshold
            ):
                lo = max(s_lo, comp_lo)
                hi = min(s_hi, comp_hi - duration)
                if lo <= hi + _eps(max(abs(lo), abs(hi))):
                    possible.append((lo, hi))

            # 必然：无读数时没有任何约束可保证浸水（总存在低于门槛的曲线），
            # 按规格必然区间为空。要求 [s, s+d] 整体落在 L>=h 集合内，
            # 与可能集合同款处理（按连通分量减去时长）。
            if readings:
                for nec_lo, nec_hi in _necessary_components(
                    readings, rate_per_ms, reading_error, threshold
                ):
                    lo = max(s_lo, nec_lo)
                    hi = min(s_hi, nec_hi - duration)
                    if lo <= hi + _eps(max(abs(lo), abs(hi))):
                        necessary.append((lo, hi))

    return {
        "extended_window": {
            "start_ms": extended_start,
            "end_ms": extended_end,
        },
        "reading_count": len(readings),
        "readings": [
            {
                "obs_id": rd["obs_id"],
                "utc_ms": rd["utc_ms"],
                "tide_level": rd["tide_level"],
            }
            for rd in readings
        ],
        "contradiction": contradiction,
        "possible_start_intervals": _emit(possible),
        "necessary_start_intervals": _emit(necessary),
    }

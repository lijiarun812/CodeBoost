#!/usr/bin/env python3
"""空间判据 v1.1：接正式 harness Result 的 Pareto 接受规则。

v1 的问题（C 在 `bb3a84d` 上核出，已修）：
1. 读顶层 `mean_trimmed_ms`，而 Result 是 `time.mean_trimmed_ms`；
2. 峰值 `None`（unmeasured）时会给出 accept，而文档要求「重测一次」；
3. 基线可测、候选降到分辨率下限以下（censored）时，这次空间改善被丢掉了；
4. 没区分「候选代码不对（incorrect）」与「测量异常（timeout/缺峰值）」。

一、三个量，三种角色
--------------------
| 量 | 角色 |
|---|---|
| `time.mean_trimmed_ms` | Pareto 轴 1（时间） |
| `mem.peak_tracemalloc_mb` | Pareto 轴 2（空间，主） |
| `mem.peak_rss_mb` | **资源上限门禁**，不是奖励轴（含解释器基线 + 5 ms 轮询采不到短用例峰值） |

二、四态决策（不是三态）
------------------------
`accept` / `reject` / **`retest`** / `incomparable`

- `retest`：**测量异常**，允许重测（`retests_used < retest_budget`）。只对
  `timeout` / `resource_exceeded` / `api_failed` / 峰值 `None` / 时间 `None` 这几种。
- `reject`：包含「候选 `incorrect`」（代码不合格，重测无意义）与「重测后仍异常」。
- `incomparable`：基线本身无效（`status != ok`），没得比。

三、可测性状态（`space_status`）
-------------------------------
`peak_tracemalloc_mb`：
- `measured`     ≥ `PEAK_TM_MIN_MB`（0.05 MB）→ 参与 Pareto
- `below_floor`  < 0.05 MB（**有数值但低于分辨力**）→ 见下
- `unmeasured`   `None`（内存轮次没跑成）→ 触发 `retest`

`peak_rss_mb`：`gated` / `indistinguishable` / `unavailable`（后两者门禁自动跳过，不否决）。

四、censoring 规则（v1 的 bug 3）
--------------------------------
- 基线 `measured`、候选 `below_floor` → **判为空间改善**（`space_better=True, censored=True`），
  只是改善幅度不可量化（只能说「降到 <0.05 MB」）；
- 基线也 `below_floor` → 空间维度退出（两边都测不出，不是改善也不是变差），退化为时间单轴。

用法（D 侧）
------------
    from eval.space import pareto_accept, space_status
    dec = pareto_accept(base_result, cand_result, rss_baseline_mb=20.52,
                        retests_used=0, retest_budget=1)
    if dec["decision"] == "retest":
        cand_result = measure_again()            # 只重测一次
        dec = pareto_accept(base_result, cand_result, rss_baseline_mb=20.52,
                            retests_used=1, retest_budget=1)
    dec["decision"]  # accept / reject / retest / incomparable
    dec["reasons"]   # 人可读，直接进日志与表格脚注

自测：`python eval/space.py`（11 条，全部用**真实 Result 结构**：`time.mean_trimmed_ms` + `mem.*`）
"""

from __future__ import annotations

PEAK_TM_MIN_MB = 0.05       # 低于这个数：空间不可分辨（与题库筛选 H1 同口径）
RSS_DELTA_MIN_MB = 5.0      # 基线高于空载基线不足这个数：RSS 门禁不生效
RSS_NOISE_MB = 1.0          # RSS 采样噪声（±0.5 MB ×2）
RSS_GATE_REL = 0.10         # 门禁：候选 RSS 不超过基线的 110%
RSS_GATE_ABS_MB = 1.0       # 或基线的 +1 MB（取两者较大）
TIME_REL_GAIN = 0.01        # 时间改善至少 1%
SPACE_REL_GAIN = 0.10       # 堆峰值改善至少 10%
SPACE_ABS_GAIN_MB = 0.005   # 或绝对 0.005 MB

RETEST_STATUSES = ("timeout", "resource_exceeded", "api_failed")
DECISIONS = ("accept", "reject", "retest", "incomparable")


# ---------------------------------------------------------------------------
# Result 读取（唯一正式路径是 schema v1.1 的嵌套结构）
# ---------------------------------------------------------------------------
def time_ms(result: dict | None) -> float | None:
    """取 `Result.time.mean_trimmed_ms`；兼容旧扁平结构（`mean_trimmed_ms`）。"""
    t = (result or {}).get("time")
    if isinstance(t, dict) and isinstance(t.get("mean_trimmed_ms"), (int, float)):
        return float(t["mean_trimmed_ms"])
    v = (result or {}).get("mean_trimmed_ms")
    return float(v) if isinstance(v, (int, float)) else None


def mem_of(result: dict | None) -> dict:
    m = (result or {}).get("mem")
    return m if isinstance(m, dict) else {}


def status_of(result: dict | None) -> str:
    return str((result or {}).get("status") or "unknown")


def time_cv(result: dict | None) -> float | None:
    """取 `Result.time.cv`；没有就 None。用于把「改善阈值」绑到实测噪声上。"""
    t = (result or {}).get("time") or {}
    cv = t.get("cv") if isinstance(t, dict) else None
    return float(cv) if isinstance(cv, (int, float)) else None


def space_status(mem: dict | None, rss_baseline_mb: float | None = None) -> dict:
    """两个峰值各自能不能用。`mem` 就是 Result.mem。"""
    mem = mem if isinstance(mem, dict) else {}
    tm = mem.get("peak_tracemalloc_mb")
    rss = mem.get("peak_rss_mb")
    if tm is None:
        tm_status, tm_reason = "unmeasured", "peak_tracemalloc_mb is None"
    elif tm < PEAK_TM_MIN_MB:
        tm_status, tm_reason = "below_floor", "%.4f MB < %.2f MB" % (tm, PEAK_TM_MIN_MB)
    else:
        tm_status, tm_reason = "measured", ""

    if rss is None:
        rss_status, rss_reason = "unavailable", "peak_rss_mb is None"
    elif rss_baseline_mb is None:
        # 没有可核对的当次基线：不是「RSS 分辨不出」，而是「我们压根没有对照」。
        # 见 docs/RSS基线规则_v1.md：门禁跳过、MU 列判 n/a，但**不停止整批**。
        rss_status, rss_reason = "unavailable", "rss_baseline_unavailable"
    elif rss - rss_baseline_mb < RSS_DELTA_MIN_MB:
        rss_status, rss_reason = "indistinguishable", (
            "%.1f − %.1f < %.1f MB" % (rss, rss_baseline_mb, RSS_DELTA_MIN_MB))
    else:
        rss_status, rss_reason = "gated", ""
    return {"peak_tm_status": tm_status, "peak_tm_reason": tm_reason,
            "rss_status": rss_status, "rss_reason": rss_reason,
            "space_measurable": tm_status == "measured"}


def _better(cand, base, rel, abs_mb=None):
    if cand is None or base is None or base <= 0:
        return False
    threshold = max(base * (1.0 - rel), base - abs_mb) if abs_mb is not None else base * (1.0 - rel)
    return cand < threshold   # 严格小于：正好等于噪声地板不算改善


def _worse(cand, base, rel, abs_mb=None):
    if cand is None or base is None or base <= 0:
        return False
    threshold = min(base * (1.0 + rel), base + abs_mb) if abs_mb is not None else base * (1.0 + rel)
    return cand > threshold   # 严格大于：正好等于噪声地板不算变差


def _result(decision, reasons, **extra):
    assert decision in DECISIONS, decision
    out = {"decision": decision, "reasons": reasons}
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# 主判据
# ---------------------------------------------------------------------------
def pareto_accept(base: dict, cand: dict, *, rss_baseline_mb: float | None = None,
                  retests_used: int = 0, retest_budget: int = 1,
                  time_rel: float = TIME_REL_GAIN,
                  space_rel: float = SPACE_REL_GAIN,
                  space_abs_mb: float = SPACE_ABS_GAIN_MB) -> dict:
    """Pareto 接受：轴 = 时间 + 堆峰值；RSS 只做门禁。返回 decision ∈ DECISIONS。

    `retests_used/retest_budget`：测量异常时最多重测 `retest_budget` 次；
    用完之后不再给 `retest`，按现有数据判定（并在 reasons 里标「重测已用完」）。
    """
    reasons: list[str] = []
    base_status, cand_status = status_of(base), status_of(cand)

    # —— 1) 基线无效：没得比
    if base_status != "ok":
        return _result("incomparable", ["基线 status=%s，不能作为比较基准" % base_status])

    # —— 2) 候选不正确：直接 reject，重测无意义
    if cand_status == "incorrect":
        return _result("reject", ["候选 status=incorrect（未通过测试），不进入效率比较"])

    # —— 3) 候选测量异常：允许重测一次
    can_retest = retests_used < retest_budget
    if cand_status in RETEST_STATUSES:
        if can_retest:
            return _result("retest", ["候选 status=%s，测量异常 → 重测一次" % cand_status],
                           retest_reason=cand_status)
        reasons.append("候选 status=%s 且重测已用完 → 判失败" % cand_status)
        return _result("reject", reasons)

    # —— 4) 读量与可测性
    t_b, t_c = time_ms(base), time_ms(cand)
    bst = space_status(mem_of(base), rss_baseline_mb)
    cst = space_status(mem_of(cand), rss_baseline_mb)

    if t_c is None:
        if can_retest:
            return _result("retest", ["候选缺 time.mean_trimmed_ms → 重测一次"],
                           retest_reason="time_missing")
        return _result("incomparable", ["候选缺 time.mean_trimmed_ms 且重测已用完"])
    if bst["peak_tm_status"] == "unmeasured" or cst["peak_tm_status"] == "unmeasured":
        if can_retest:
            return _result("retest", ["峰值缺值（%s / %s）→ 重测一次"
                                      % (bst["peak_tm_status"], cst["peak_tm_status"])],
                           retest_reason="peak_missing")
        reasons.append("峰值缺值且重测已用完 → 空间维度退出，只看时间")

    # —— 5) 空间轴（含 censoring）
    space_better = space_worse = censored = False
    space_axis = False
    if (bst["peak_tm_status"] == "measured" and cst["peak_tm_status"] == "measured"
            and bst["peak_tm_status"] != "unmeasured" and cst["peak_tm_status"] != "unmeasured"):
        space_axis = True
        s_b, s_c = mem_of(base)["peak_tracemalloc_mb"], mem_of(cand)["peak_tracemalloc_mb"]
        space_better = _better(s_c, s_b, space_rel, space_abs_mb)
        space_worse = _worse(s_c, s_b, space_rel, space_abs_mb)
        if space_better:
            reasons.append("堆峰值改善 %.1f%%" % ((s_b - s_c) / s_b * 100.0))
        if space_worse:
            reasons.append("堆峰值变差 %.1f%%" % ((s_c - s_b) / s_b * 100.0))
    elif bst["peak_tm_status"] == "measured" and cst["peak_tm_status"] == "below_floor":
        # 基线可测、候选掉到下限以下：这是**改善**，只是幅度不可量化（censored）
        space_axis = True
        space_better, censored = True, True
        reasons.append("堆峰值降到分辨下限以下（<%.2f MB）→ 记为改善但幅度不可量化"
                       % PEAK_TM_MIN_MB)
    elif bst["peak_tm_status"] == "below_floor":
        reasons.append("基线本身低于分辨下限（%.4f MB）→ 空间维度退出，只看时间"
                       % (mem_of(base).get("peak_tracemalloc_mb") or 0.0))

    # —— 6) 时间轴（阈值绑到实测噪声：max(常数, 2*CV)）
    cv_b = time_cv(base)
    time_rel_eff = max(time_rel, 2.0 * cv_b) if cv_b is not None else time_rel
    time_better = _better(t_c, t_b, time_rel_eff)
    time_worse = _worse(t_c, t_b, time_rel_eff)
    if time_better:
        reasons.append("时间改善 %.1f%%（阈值 %.1f%%）"
                       % ((t_b - t_c) / t_b * 100.0, time_rel_eff * 100.0))
    if time_worse:
        reasons.append("时间变差 %.1f%%（阈值 %.1f%%）"
                       % ((t_c - t_b) / t_b * 100.0, time_rel_eff * 100.0))

    # —— 7) RSS 门禁（只在 gated 时生效）
    gate_failed = False
    if bst["rss_status"] == "gated" and cst["rss_status"] in ("gated", "indistinguishable"):
        b_rss, c_rss = mem_of(base).get("peak_rss_mb"), mem_of(cand).get("peak_rss_mb")
        if c_rss is not None:
            limit = max(b_rss * (1.0 + RSS_GATE_REL), b_rss + RSS_GATE_ABS_MB)
            if c_rss > limit:
                gate_failed = True
                reasons.append("RSS 门禁未过：%.1f MB > 上限 %.1f MB" % (c_rss, limit))
    else:
        reasons.append("RSS 门禁跳过（%s）" % bst["rss_status"])

    # —— 8) 汇总
    if time_worse or space_worse or gate_failed:
        decision = "reject"
    elif time_better or space_better:
        decision = "accept"
    else:
        decision = "reject"
        reasons.append("两轴都没有超出噪声的改善 → 保持")
    return _result(decision, reasons,
                   space_axis=space_axis, censored=censored,
                   space_below_floor=bst["peak_tm_status"] == "below_floor",
                   time_better=time_better, space_better=space_better,
                   time_worse=time_worse, space_worse=space_worse,
                   gate_failed=gate_failed,
                   base_peak_tm=bst["peak_tm_status"],
                   cand_peak_tm=cst["peak_tm_status"],
                   rss_status=bst["rss_status"])


# ---------------------------------------------------------------------------
# 自测：一律用真实 Result 结构（time.mean_trimmed_ms / mem.*）
# ---------------------------------------------------------------------------
def _R(status="ok", t=10.0, tm=2.0, rss=60.0):
    return {"status": status,
            "error": None,
            "time": {"mean_trimmed_ms": t, "median_ms": t, "raw_ms": [t]},
            "mem": {"peak_tracemalloc_mb": tm, "peak_rss_mb": rss},
            "manifest": {"protocol_version": "proto-2026.11"}}


def _selftest():
    base = _R(t=10.0, tm=2.0, rss=60.0)

    print("== 1) 读的是 time.mean_trimmed_ms（真实 Result 结构） ==")
    d = pareto_accept(base, _R(t=8.0, tm=1.0, rss=60.0), rss_baseline_mb=20.0)
    assert d["decision"] == "accept" and d["time_better"], d
    print("  ok:", d["reasons"])

    print("== 2) 时间改善但堆峰值变差 -> reject（Pareto 硬边界） ==")
    d = pareto_accept(base, _R(t=7.0, tm=3.0, rss=60.0), rss_baseline_mb=20.0)
    assert d["decision"] == "reject" and d["space_worse"], d
    print("  ok:", d["reasons"])

    print("== 3) 候选峰值 None -> retest（不再直接 accept） ==")
    d = pareto_accept(base, _R(t=8.0, tm=None, rss=60.0), rss_baseline_mb=20.0)
    assert d["decision"] == "retest", d
    print("  ok:", d["reasons"])
    print("== 3b) 重测用完仍缺峰值 -> 按时间判定并标记降级 ==")
    d = pareto_accept(base, _R(t=8.0, tm=None, rss=60.0), rss_baseline_mb=20.0,
                      retests_used=1, retest_budget=1)
    assert d["decision"] == "accept" and d["space_axis"] is False, d
    print("  ok:", d["reasons"])

    print("== 4) 基线可测、候选掉到下限以下 -> accept 且 censored ==")
    d = pareto_accept(base, _R(t=10.0, tm=0.01, rss=60.0), rss_baseline_mb=20.0)
    assert d["decision"] == "accept" and d["censored"] and d["space_better"], d
    print("  ok:", d["reasons"])

    print("== 5) 候选 incorrect -> reject（重测无意义） ==")
    d = pareto_accept(base, _R(status="incorrect", t=8.0, tm=1.0), rss_baseline_mb=20.0)
    assert d["decision"] == "reject", d
    print("  ok:", d["reasons"])

    print("== 6) 候选 timeout -> retest；重测后仍 timeout -> reject ==")
    d = pareto_accept(base, _R(status="timeout"), rss_baseline_mb=20.0)
    assert d["decision"] == "retest", d
    d = pareto_accept(base, _R(status="timeout"), rss_baseline_mb=20.0,
                      retests_used=1, retest_budget=1)
    assert d["decision"] == "reject", d
    print("  ok:", d["reasons"])

    print("== 7) 基线非 ok -> incomparable ==")
    d = pareto_accept(_R(status="timeout"), _R(t=8.0, tm=1.0))
    assert d["decision"] == "incomparable", d
    print("  ok:", d["reasons"])

    print("== 8) 两边都低于下限 -> 空间退出，只看时间 ==")
    d = pareto_accept(_R(t=10.0, tm=0.001, rss=20.7), _R(t=7.0, tm=0.001, rss=20.7),
                      rss_baseline_mb=20.5)
    assert d["decision"] == "accept" and d["space_below_floor"], d
    print("  ok:", d["reasons"])

    print("== 9) RSS 不可分辨 -> 门禁跳过（不否决） ==")
    d = pareto_accept(_R(t=10.0, tm=2.0, rss=21.0), _R(t=8.0, tm=1.0, rss=21.5),
                      rss_baseline_mb=20.5)
    assert d["decision"] == "accept" and not d["gate_failed"], d
    print("  ok:", d["reasons"])

    print("== 10) RSS 可分辨且明显变胖 -> 门禁否决 ==")
    d = pareto_accept(base, _R(t=8.0, tm=1.0, rss=90.0), rss_baseline_mb=20.0)
    assert d["decision"] == "reject" and d["gate_failed"], d
    print("  ok:", d["reasons"])

    print("== 12) 时间阈值要绑实测噪声：CV=4.5% 时 7.9% 的\"改善\"算噪声 -> reject ==")
    noisy_b = _R(t=0.63, tm=0.0013, rss=20.7)
    noisy_b["time"]["cv"] = 0.045
    noisy_c = _R(t=0.58, tm=0.0013, rss=20.7)    # 快 7.9%，落在 2*CV=9% 噪声带内
    d = pareto_accept(noisy_b, noisy_c, rss_baseline_mb=20.52)
    assert d["decision"] == "reject", d
    print("  ok:", d["reasons"])
    d = pareto_accept(noisy_b, _R(t=0.50, tm=0.0013, rss=20.7), rss_baseline_mb=20.52)  # 快 21%
    assert d["decision"] == "accept", d
    print("  ok:", d["reasons"])

    print("== 13) 没有空载基线 -> rss_status=unavailable，门禁跳过但仍可判定 ==")
    d = pareto_accept(base, _R(t=8.0, tm=1.0, rss=60.0), rss_baseline_mb=None)
    assert d["decision"] == "accept" and d["rss_status"] == "unavailable", d
    print("  ok:", d["reasons"])

    print("== 11) 扁平旧结构仍可读（向后兼容，非正式路径） ==")
    flat_b = {"status": "ok", "mean_trimmed_ms": 10.0,
              "mem": {"peak_tracemalloc_mb": 2.0, "peak_rss_mb": 60.0}}
    flat_c = {"status": "ok", "mean_trimmed_ms": 8.0,
              "mem": {"peak_tracemalloc_mb": 1.0, "peak_rss_mb": 60.0}}
    d = pareto_accept(flat_b, flat_c, rss_baseline_mb=20.0)
    assert d["decision"] == "accept", d
    print("  ok:", d["reasons"])

    print("\nALL SPACE SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())

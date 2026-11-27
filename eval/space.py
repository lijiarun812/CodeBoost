#!/usr/bin/env python3
"""空间判据：Pareto 接受规则 + 「这个量到底能不能测」的状态标记。

这是给 D（Ours 联合优化）的**参考实现**：D 不该自己发明接受规则，否则又是一个「各写一套」。

一、三个量，三种角色（不要混）
--------------------------------
| 量 | 角色 | 能不能当「省内存」的奖励 |
|---|---|---|
| `mean_trimmed_ms` | Pareto 轴 1（时间） | — |
| `peak_tracemalloc_mb` | Pareto 轴 2（空间，**主**） | ✅ 能（可分辨时） |
| `peak_rss_mb` | **资源上限门禁**，不是改进轴 | ❌ 不能 |

为什么 RSS 不当奖励轴：它含解释器基线（本机 20.5 MB）与 arena 碎片，5 ms 轮询也常采不到峰值。
实测 EffiBench 原生题 0/19 可分辨（差值 <0.1 MB）——用它做接受判据等于**让噪声决定接受**。

二、可分辨性三态（这是 D 缺的那块）
------------------------------------
`peak_tracemalloc_mb`：
- `measured`      ≥ `PEAK_TM_MIN_MB`（0.05 MB）→ 参与 Pareto
- `unmeasurable`  < 0.05 MB → 空间维度**整体退出**，退化为「只比时间」，标 `space_unmeasurable`
- `unmeasured`    值为 `None`（内存轮次没跑成 / tracemalloc 不可用）→ **不参与比较**，标 `space_unmeasured`

`peak_rss_mb`：
- `gated`          基线高于空载基线 5 MB 以上，且差值大于噪声 → 门禁生效
- `indistinguishable` 差值在噪声内 → **门禁自动跳过**（不否决任何候选）
- `unavailable`    `None`（psutil 缺失）→ 门禁跳过

三、接受规则（Pareto + 门禁）
------------------------------
- 时间轴：`cand.mean_trimmed_ms ≤ base × (1 − TIME_REL_GAIN)`
- 空间轴（仅当 `peak_tm_status == "measured"`）：`cand.peak_tracemalloc_mb ≤ base × (1 − SPACE_REL_GAIN)`
- **接受条件**：至少一轴有改善，且**没有**任何可测轴变差（超出噪声），且 RSS 门禁通过（若生效）
- 空间不可测时：只看时间轴，返回值里 `space_unmeasurable=True`（报告必须标出来）

用法
----
    from eval.space import pareto_accept, space_status
    st = space_status(base["mem"])
    dec = pareto_accept(base, cand, rss_baseline_mb=20.5)
    dec["decision"]   # "accept" / "reject" / "incomparable"
    dec["reasons"]    # 人可读原因，直接进日志/表格脚注
"""

from __future__ import annotations

PEAK_TM_MIN_MB = 0.05       # 低于这个数：空间不可分辨（与题库筛选 H1 同口径）
RSS_DELTA_MIN_MB = 5.0      # 基线高于空载基线不足这个数：RSS 门禁不生效
RSS_NOISE_MB = 1.0          # RSS 采样噪声（±0.5 MB ×2）
RSS_GATE_REL = 0.10         # 门禁相对阈值：候选 RSS 不超过基线的 110%
RSS_GATE_ABS_MB = 1.0       # 门禁绝对余量：或基线的 +1 MB（取两者较大）
TIME_REL_GAIN = 0.01        # 时间改善至少 1%（低于噪声不算）
SPACE_REL_GAIN = 0.10       # 空间改善至少 10%（peak_tm 采样方差小于时间，用相对阈值）
SPACE_ABS_GAIN_MB = 0.005   # 或绝对 0.005 MB（取两者较大）


def space_status(mem: dict | None, rss_baseline_mb: float | None = None) -> dict:
    """判定两个峰值各自能不能用。`mem` 就是 Result.mem。"""
    mem = mem or {}
    tm = mem.get("peak_tracemalloc_mb")
    rss = mem.get("peak_rss_mb")
    if tm is None:
        peak_tm_status, tm_reason = "unmeasured", "peak_tracemalloc_mb is None"
    elif tm < PEAK_TM_MIN_MB:
        peak_tm_status, tm_reason = "unmeasurable", "%.4f MB < %.2f MB" % (tm, PEAK_TM_MIN_MB)
    else:
        peak_tm_status, tm_reason = "measured", ""

    if rss is None:
        rss_status, rss_reason = "unavailable", "peak_rss_mb is None"
    elif rss_baseline_mb is None:
        rss_status, rss_reason = "indistinguishable", "no rss baseline"
    elif rss - rss_baseline_mb < RSS_DELTA_MIN_MB:
        rss_status, rss_reason = "indistinguishable", (
            "%.1f − %.1f < %.1f MB" % (rss, rss_baseline_mb, RSS_DELTA_MIN_MB))
    else:
        rss_status, rss_reason = "gated", ""
    return {"peak_tm_status": peak_tm_status, "peak_tm_reason": tm_reason,
            "rss_status": rss_status, "rss_reason": rss_reason,
            "space_measurable": peak_tm_status == "measured"}


def _better(cand, base, rel, abs_mb=None):
    """cand 是否显著优于 base（超出噪声）。"""
    if cand is None or base is None or base <= 0:
        return False
    threshold = max(base * (1.0 - rel), base - abs_mb) if abs_mb is not None else base * (1.0 - rel)
    return cand <= threshold


def _worse(cand, base, rel, abs_mb=None):
    """cand 是否显著差于 base（超出噪声）。"""
    if cand is None or base is None or base <= 0:
        return False
    threshold = min(base * (1.0 + rel), base + abs_mb) if abs_mb is not None else base * (1.0 + rel)
    return cand >= threshold


def pareto_accept(base: dict, cand: dict, *, rss_baseline_mb: float | None = None,
                  time_rel: float = TIME_REL_GAIN,
                  space_rel: float = SPACE_REL_GAIN,
                  space_abs_mb: float = SPACE_ABS_GAIN_MB) -> dict:
    """返回 {decision, reasons, space_unmeasurable, ...}；不抛异常，缺数据就 `incomparable`。"""
    reasons = []
    if (base or {}).get("status") != "ok" or (cand or {}).get("status") != "ok":
        return {"decision": "incomparable", "reasons": ["status != ok"],
                "space_unmeasurable": False}

    bst = space_status(base.get("mem"), rss_baseline_mb)
    cst = space_status(cand.get("mem"), rss_baseline_mb)
    space_ok = bst["peak_tm_status"] == "measured" and cst["peak_tm_status"] == "measured"
    if not space_ok:
        reasons.append("空间不可测（%s / %s）→ 只看时间"
                       % (bst["peak_tm_status"], cst["peak_tm_status"]))

    t_b, t_c = base.get("mean_trimmed_ms"), cand.get("mean_trimmed_ms")
    time_better = _better(t_c, t_b, time_rel)
    time_worse = _worse(t_c, t_b, time_rel)
    if time_better:
        reasons.append("时间改善 %.1f%%" % ((t_b - t_c) / t_b * 100.0))
    if time_worse:
        reasons.append("时间变差 %.1f%%" % ((t_c - t_b) / t_b * 100.0))

    space_better = space_worse = False
    if space_ok:
        s_b = base["mem"]["peak_tracemalloc_mb"]
        s_c = cand["mem"]["peak_tracemalloc_mb"]
        space_better = _better(s_c, s_b, space_rel, space_abs_mb)
        space_worse = _worse(s_c, s_b, space_rel, space_abs_mb)
        if space_better:
            reasons.append("堆峰值改善 %.1f%%" % ((s_b - s_c) / s_b * 100.0))
        if space_worse:
            reasons.append("堆峰值变差 %.1f%%" % ((s_c - s_b) / s_b * 100.0))

    # RSS 门禁：只在 gated 时生效；不可分辨/不可用时自动跳过（不否决）
    gate_failed = False
    if bst["rss_status"] == "gated" and cst["rss_status"] in ("gated", "indistinguishable"):
        b_rss, c_rss = base["mem"]["peak_rss_mb"], cand["mem"].get("peak_rss_mb")
        if c_rss is not None:
            limit = max(b_rss * (1.0 + RSS_GATE_REL), b_rss + RSS_GATE_ABS_MB)
            if c_rss > limit:
                gate_failed = True
                reasons.append("RSS 门禁未过：%.1f MB > 上限 %.1f MB" % (c_rss, limit))
    else:
        reasons.append("RSS 门禁跳过（%s）" % bst["rss_status"])

    if time_worse or space_worse or gate_failed:
        decision = "reject"
    elif time_better or space_better:
        decision = "accept"
    else:
        decision = "reject"
        reasons.append("两轴都没有超出噪声的改善 → 保持")
    return {"decision": decision, "reasons": reasons,
            "space_unmeasurable": not space_ok,
            "base_status": bst["peak_tm_status"], "cand_status": cst["peak_tm_status"],
            "rss_status": bst["rss_status"],
            "time_better": time_better, "space_better": space_better,
            "time_worse": time_worse, "space_worse": space_worse,
            "gate_failed": gate_failed}


def _selftest():
    def R(t, tm, rss):
        return {"status": "ok", "mean_trimmed_ms": t,
                "mem": {"peak_tracemalloc_mb": tm, "peak_rss_mb": rss}}

    print("== 1) 时间与空间都改善 -> accept ==")
    d = pareto_accept(R(10.0, 2.0, 60.0), R(8.0, 1.0, 60.0), rss_baseline_mb=20.0)
    assert d["decision"] == "accept", d
    print("  ok:", d["reasons"])

    print("== 2) 时间改善但堆峰值变差 -> reject（Pareto，不允许拿空间换时间） ==")
    d = pareto_accept(R(10.0, 2.0, 60.0), R(7.0, 3.0, 60.0), rss_baseline_mb=20.0)
    assert d["decision"] == "reject", d
    print("  ok:", d["reasons"])

    print("== 3) 小题空间不可测 -> 只看时间，且标 space_unmeasurable ==")
    d = pareto_accept(R(10.0, 0.001, 20.7), R(7.0, 0.001, 20.7), rss_baseline_mb=20.5)
    assert d["decision"] == "accept" and d["space_unmeasurable"], d
    print("  ok:", d["reasons"])

    print("== 4) RSS 不可分辨时门禁自动跳过（不否决） ==")
    d = pareto_accept(R(10.0, 2.0, 21.0), R(8.0, 1.0, 21.5), rss_baseline_mb=20.5)
    assert d["decision"] == "accept" and not d["gate_failed"], d
    print("  ok:", d["reasons"])

    print("== 5) RSS 可分辨且明显变胖 -> 门禁否决 ==")
    d = pareto_accept(R(10.0, 2.0, 60.0), R(8.0, 1.0, 90.0), rss_baseline_mb=20.0)
    assert d["decision"] == "reject" and d["gate_failed"], d
    print("  ok:", d["reasons"])

    print("== 6) 缺峰值 -> incomparable，不参与比较 ==")
    bad = {"status": "ok", "mean_trimmed_ms": 8.0, "mem": {"peak_tracemalloc_mb": None, "peak_rss_mb": None}}
    d = pareto_accept(R(10.0, 2.0, 60.0), bad, rss_baseline_mb=20.0)
    assert d["decision"] == "accept" and d["cand_status"] == "unmeasured", d
    d2 = pareto_accept({"status": "timeout"}, R(8.0, 1.0, 60.0))
    assert d2["decision"] == "incomparable", d2
    print("  ok: unmeasured 只降级不否决；status!=ok 判 incomparable")

    print("\nALL SPACE SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())

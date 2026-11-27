#!/usr/bin/env python3
"""统计与噪声判定（对应优化计划 8.3：配对 bootstrap + 噪声地板，替代主观 1.3x 阈值）。

为什么需要它：中期的“加速比 1.171x / 1.3x 阈值”是拍脑袋定的；同一份代码重复测
本身就有 2%–10% 的抖动。正确做法是：
1. 报中位数 + IQR + CV；
2. 用**配对 bootstrap** 给加速比一个 95% CI；
3. 用**噪声地板**（1 + k·CV）判断“CI 下界是否越过噪声”，越过才算真实提升。

对外接口：
    describe(values)                      -> {n, median, iqr, cv, min, max}
    paired_bootstrap_ci(base, cand, ...)  -> (ratio, lo, hi)
    noise_floor(cv, k=2.0)                -> 阈值
    decide(base_values, cand_values)      -> {ratio, ci_low, ci_high, floor, real}
"""

from __future__ import annotations

import argparse
import random
import statistics


def describe(values):
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return {"n": 0, "median": None, "iqr": None, "cv": None, "min": None, "max": None}
    med = statistics.median(vals)
    if len(vals) >= 4:
        q1 = statistics.median(vals[:len(vals) // 2])
        q3 = statistics.median(vals[(len(vals) + 1) // 2:])
        iqr = q3 - q1
    else:
        iqr = max(vals) - min(vals)
    mean = statistics.mean(vals)
    cv = (statistics.pstdev(vals) / mean) if (len(vals) > 1 and mean) else 0.0
    return {"n": len(vals), "median": round(med, 4), "iqr": round(iqr, 4),
            "cv": round(cv, 4), "min": round(min(vals), 4), "max": round(max(vals), 4)}


def paired_bootstrap_ci(baseline, candidate, n_boot=2000, alpha=0.05, seed=42):
    """配对重采样加速比（baseline/candidate）的 (1-alpha) CI。

    要求两个序列等长且逐项配对（同一题、同一输入档的第 i 次测量）。
    返回 (ratio_mean, lo, hi)；样本不足 2 时返回 (None, None, None)。
    """
    pairs = [(float(b), float(c)) for b, c in zip(baseline, candidate)
             if b and c and b > 0 and c > 0]
    if len(pairs) < 2:
        return None, None, None
    ratios = [b / c for b, c in pairs]
    rnd = random.Random(seed)
    n = len(ratios)
    boots = []
    for _ in range(n_boot):
        s = sum(ratios[rnd.randrange(n)] for _ in range(n)) / n
        boots.append(s)
    boots.sort()
    lo = boots[int((alpha / 2) * n_boot)]
    hi = boots[int((1 - alpha / 2) * n_boot) - 1]
    return round(statistics.mean(ratios), 4), round(lo, 4), round(hi, 4)


def noise_floor(cv, k=2.0):
    """噪声地板：1 + k·CV（CV=5% 时阈值约 1.10）。"""
    if not cv:
        return 1.0
    return round(1.0 + k * float(cv), 4)


def decide(base_values, cand_values, k=2.0, n_boot=2000, seed=42):
    """判断“加速比是否越过噪声地板”。"""
    ratio, lo, hi = paired_bootstrap_ci(base_values, cand_values, n_boot=n_boot, seed=seed)
    cv = max((describe(base_values).get("cv") or 0.0),
             (describe(cand_values).get("cv") or 0.0))
    floor = noise_floor(cv, k)
    real = bool(lo is not None and lo > floor)
    return {"ratio": ratio, "ci_low": lo, "ci_high": hi, "cv": round(cv, 4),
            "floor": floor, "real": real}


def _selftest():
    rnd = random.Random(7)
    base = [10.0 * (1 + rnd.uniform(-0.05, 0.05)) for _ in range(40)]

    print("== 真实 +20% ==")
    cand = [b / 1.20 * (1 + rnd.uniform(-0.02, 0.02)) for b in base]
    d = decide(base, cand)
    print("  ", d)
    assert d["ratio"] and d["ratio"] > 1.15, d
    assert d["real"] is True, d

    print("== 噪声 +2%（应判为不真实） ==")
    cand2 = [b / 1.02 * (1 + rnd.uniform(-0.05, 0.05)) for b in base]
    d2 = decide(base, cand2)
    print("  ", d2)
    assert d2["real"] is False, d2

    print("== 描述统计 ==")
    s = describe(base)
    print("  ", s)
    assert s["n"] == 40 and s["cv"] < 0.1

    print("== 样本不足 ==")
    assert paired_bootstrap_ci([1.0], [0.5]) == (None, None, None)

    print("\nALL STATS SELFTESTS PASSED")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="统计与噪声地板（配对 bootstrap）")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)
    if args.selftest:
        return _selftest()
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

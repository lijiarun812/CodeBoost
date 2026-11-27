"""证明 mem_mbs(∫RSS dt) 不是内存指标，而是「耗时 × 常驻内存」的耗时代理。

用法：py eval/check_mem_metric.py

同一函数，只改变计算量（不改变内存占用），观察 mem_mbs 如何变化。
本机实测（reps=20000）：

    变体        time(s)    mem_mbs   peak_mb
    fast        0.0014     0.0000    22.55
    slow1.5x    0.3506     7.6918    22.48
    slow3x      1.6368    36.7395    22.53

三点结论：
1. 三种变体峰值内存几乎相同（差 <0.4%），mem_mbs 却差 3 个数量级；
2. mem_mbs / exec_time ≈ 21.9 / 22.4 MB，与 peak_mb 一致 —— 即 mem_mbs ≈ 峰值 × 耗时；
3. 代码足够快时基线 mem_mbs 恰为 0，于是「仅当 mem_mbs 更小才采纳」等价于
   「候选必须零分配」，判据在结构上必然拒绝绝大多数候选。

所以：EffiLearner 论文口径下的「内存降幅」实为耗时降幅的投影，
不能作为空间优化证据；空间必须改用峰值口径（peak_tracemalloc / peak_rss）。
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from effilearner.effi_repro import measure  # noqa: E402

FAST = "def f(n):\n    return n\n"
SLOW_1_5X = "def f(n):\n    x = 0\n    for i in range(n // 2):\n        x += i\n    return n\n"
SLOW_3X = "def f(n):\n    x = 0\n    for i in range(n * 2):\n        x += i\n    return n\n"
TEST = "assert f(1000) == 1000"


def main():
    wd = Path(tempfile.mkdtemp(prefix="memmetric_"))
    print("%-10s %10s %10s %10s %10s" % ("变体", "time(s)", "mem_mbs", "peak_mb", "mem/time"))
    base = None
    for name, code in (("fast", FAST), ("slow1.5x", SLOW_1_5X), ("slow3x", SLOW_3X)):
        m = measure(code, TEST, 20000, 30, wd)
        if not m.get("ok"):
            print("%-10s 失败: %s" % (name, m.get("error")))
            continue
        base = base or m
        ratio = (m["mem_mbs"] / m["exec_time"]) if m["exec_time"] else 0.0
        print("%-10s %10.4f %10.4f %10.4f %10.2f   time x%.1f  mem x%.2f"
              % (name, m["exec_time"], m["mem_mbs"], m["peak_mb"], ratio,
                 m["exec_time"] / base["exec_time"],
                 (m["mem_mbs"] / base["mem_mbs"]) if base["mem_mbs"] else float("inf")))
    print("\nmem/time 列 ≈ peak_mb ⇒ mem_mbs 只是耗时与常驻内存的乘积，不含独立的空间信息。")


if __name__ == "__main__":
    main()

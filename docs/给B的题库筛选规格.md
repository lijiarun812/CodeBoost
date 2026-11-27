# 给 B：题库筛选规格（空间可分辨 + 时间可测）

> 出规格：队员A（`eval/` 负责人） · 日期 2026-11-27 · 依据：`proto-2026.11` 测量协议
> 目的：冻结一份 **120–200 题**的题库，让「同时优化空间」这件事**在测量上站得住**。

---

## 一、为什么必须加「空间可分辨」这条

我们实测了 24 题（EffiBench，canonical solution，本机 `C:\Python313`）：

| tracemalloc 峰值 | 题数 |
|---|---|
| 中位 | 0.0017 MB |
| 最大 | 1.3675 MB |
| ≥ 0.1 MB | 5 / 22（23%） |
| ≥ 0.01 MB | 8 / 22 |

而 `peak_rss`（对齐论文 MU 的那一列）**全部贴在 20.5 MB 的空载基线上**，差值 <0.1 MB。
结论：**不筛题的后果就是「空间列全是 0」，Ours 的「同时优化空间」讲不出任何数字。**

按 23% 的通过率倒推：**要凑 200 题，至少得测 ~800 题**。这是 B 的时间预算，请先估「800 题 × 每题 3 次测量」要跑多久，再动手。

---

## 二、三条条件（**已用 30 题实测校准，别用旧的 20 ms**）

先说结论：**旧的「`baseline_ms ≥ 20 ms`」在 EffiBench 原生用例上基本不可能满足**（30 题里只有 1 题，3%），
按那个门槛筛，你会得到近乎空集。实测分布（`results/_peak_sample/scan36.json`，30 题）：

| 条件 | 通过率 |
|---|---|
| `peak_tm ≥ 0.05 MB`（空间） | **8/30 = 27%** |
| `peak_rss − 20.5 ≥ 5 MB`（MU） | 2/30 = 7% |
| `baseline_ms ≥ 20 ms` 且 `cv ≤ 0.10` | **1/30 = 3%** |
| `baseline_ms ≥ 1 ms` 且 `cv ≤ 0.10` | 9/30 = 30% |
| **空间 ∩ 时间（`≥ 1 ms`）** | **5/30 ≈ 17%** |

`baseline_ms` 中位数只有 **0.226 ms**、最大 51.3 ms——这批题本来就小。

| # | 条件 | 门槛 | 是否硬门槛 |
|---|---|---|---|
| **H1** | 空间可分辨 | `peak_tracemalloc_mb ≥ 0.05`（≥0.1 更稳） | ✅ **主门槛** |
| **H2** | MU 可分辨 | `peak_rss_mb − 20.5 ≥ 5.0` | ❌ 只记录（7%，太严）；但清单里必须有这一列 |
| **H3'** | 时间可测 | `baseline_ms ≥ 1.0` **且** `cv ≤ 0.10` | ✅ 硬门槛（旧的 20 ms 作废） |

**预期产出**：H1 ∩ H3' ≈ **17%**。1000 题全测 ≈ **170 题**，正好落在 120–200；
只要 120 题的话测 ~700 题就够。**这是你的时间预算依据。**

**排序建议**：在命中集合里按 `peak_tm_mb` 降序取（空间信号最强的优先），
同档内按 `baseline_ms` 降序（大题目对空间更友好）。

---

## 三、判定脚本（可直接跑，别自己重写测量）

```python
# python tools/grade_bank.py --dataset datasets/dataset.json --out bank/space_resolved.json
import json, sys
from pathlib import Path
sys.path.insert(0, ".")                 # 仓库根
from eval import harness

BASELINE_MB = 20.5      # 空载 RSS 基线（dual_report 会自动量，这里取缓存值）
H1_PEAK_TM = 0.05       # MB
H2_RSS_DELTA = 5.0      # MB（只记录，不作门槛）
H3_MIN_MS, H3_MAX_CV = 1.0, 0.10   # 旧值 20 ms 实测不可达（3%），已降到 1 ms

def grade(entry):
    code = entry["canonical_solution"]
    m = harness.measure_code(
        code, entry["test_case"], kind="test_block", entry="solution",
        e_reps=harness.E_REPS, target_batch_s=0.25, max_reps=20000,
    )
    if m.get("status") != "ok":
        return None, m.get("status"), m.get("error")
    peak_tm = (m.get("mem") or {}).get("peak_tracemalloc_mb") or 0.0
    peak_rss = (m.get("mem") or {}).get("peak_rss_mb") or 0.0
    base_ms = m.get("mean_trimmed_ms") or 0.0
    cv = (m.get("stats") or {}).get("cv") or 1.0
    return {
        "problem_idx": entry["problem_idx"],
        "task_name": entry["task_name"],
        "peak_tm_mb": round(peak_tm, 6),
        "peak_rss_mb": round(peak_rss, 2),
        "rss_delta_mb": round(max(0.0, peak_rss - BASELINE_MB), 2),
        "baseline_ms": round(base_ms, 4),
        "cv": round(cv, 4),
        "pass_h1_space": peak_tm >= H1_PEAK_TM,
        "pass_h2_mu": max(0.0, peak_rss - BASELINE_MB) >= H2_RSS_DELTA,
        "pass_h3_time": base_ms >= H3_MIN_MS and cv <= H3_MAX_CV,
    }, "ok", None
```

跑完把结果落到一个 JSON 数组里，字段就用上面那几个——**不要自己加 `difficulty` 文本标签**，
难度一律用 `baseline_ms / peak_tm_mb / rss_delta_mb` 三个数客观表达（EffiBench 原始数据里没有 difficulty）。

---

## 四、必须排除的三类题

1. **`class Solution` 缺失的题**（EffiBench 里有若干）：`test_case_generator` 里没有 `class Solution`，
   `load_problems()` 会直接丢弃，别手动捞。
2. **原生测试块本身写坏的题**：例如 `problem_idx=250 Count Univalue Subtrees` 的
   `assert solution.countUnivalSubtrees(TreeNode) == 393` 把**类**当实例传，canonical solution 也过不了 →
   `status=incorrect`。这类题要在筛的时候直接判掉（`status != ok` 即淘汰）。
3. **只有 `small_test_cases` 没有稳定断言的题**。

---

## 五、产出与交接

- 文件：`bank/space_resolved.json`（数组，字段见 §三），另外附一页 `bank/README.md` 写清：跑了几题、命中多少、用的哪台机器、`protocol_version`；
- **不要改 `eval/`**，任何字段需求提给我；
- 冻结后把清单交给组长：矩阵跑完后 `eval/dual_report.py` 会按 `problem_idx` 自动对齐，空间列只在这个子集上出数。

---

## 六、两个必须避免的坑

1. **不要用 `mem_mbs` 筛题**——它是 `∫RSS dt ≈ 峰值 × 耗时`，本质是耗时，筛出来的会是「慢题」而不是「大内存题」；
2. **不要指望 `eval/inputs.py` 的 gen200/gen1000**——那两档只扩**用例数量**，不扩**输入尺寸**，对峰值没有帮助。
   输入规模升档是我这边的活（`n: 1e3→1e4→1e5`，只进 `diagnostic_only`），做好之前你先只管筛题。

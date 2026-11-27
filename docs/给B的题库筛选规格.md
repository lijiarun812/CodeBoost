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

而 `peak_rss`（对齐论文 MU 的那一列）在本次 40 题里**全部贴在 20.5 MB 的空载基线上**，差值 <0.1 MB。
这个「没有信号」有**两种可能原因，暂未区分**：
① **测量分辨率**——RSS 是进程级采样（±0.5 MB），扣掉基线后小题只剩噪声；tracemalloc 的亚 KB 峰值同理；
② **输入规模**——题目本身的输入太小，一次调用真的不分配多少内存。
（这两条要靠「输入规模升档」实验才能分开，A 在做。）
结论：**不筛题的后果就是「空间列全是 0」，Ours 的「同时优化空间」讲不出任何数字。**

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

| # | 条件 | 门槛（**暂定值**） | 是否硬门槛 |
|---|---|---|---|
| **H1** | 空间可分辨 | `peak_tracemalloc_mb ≥ 0.05` MB | ✅ **主门槛** |
| **H2** | MU 可分辨 | `peak_rss_mb − 基线 ≥ 5.0` MB | ❌ 只记录（太严），但清单里必须有这一列 |
| **H3'** | 时间可测 | `baseline_ms ≥ 1.0` **且** `cv ≤ 0.10` | ✅ 硬门槛（旧的 20 ms 作废） |

> ⚠️ **这三个数是「30 题试测」得出的暂定门槛，不是冻结值。**
> 30 题里 H1 命中 8 题、H1∩H3' 命中 5 题（17%）——**这个 17% 不能直接保证 1000 题会产出约 170 题**，
> 样本太小、题目难度分布也不均匀。**请你在更大的样本上校准这三个门槛**（比如先扫 100–150 题），
> 校准完把「实际命中率 + 建议门槛」回报给 A，由 A 更新本规格。

**产出预期（按试测外推，仅供参考）**：H1 ∩ H3' ≈ 17%，1000 题量级**可能**落在 100–200 题区间；
但这必须由你在更大样本上验证。只要 120 题的话，建议先扫 300 题再决定要不要全量。

**排序建议**：在命中集合里按 `peak_tm_mb` 降序取（空间信号最强的优先），
同档内按 `baseline_ms` 降序（大题目对空间更友好）。

---

## 三、判定脚本（已入库，别自己重写测量）

脚本：`tools/scan_space_signal.py`（走 `eval/harness.py`，不含任何自建计时逻辑）。

```bash
# 先扫 40 题看通过率，再决定要不要全量（推荐）
python tools/scan_space_signal.py --num 40

# 全量扫描，逐题写 JSONL，可断点续跑
python tools/scan_space_signal.py --start 0 --out results/bank_scan.jsonl

# 门槛可调（默认就是上文暂定值）
python tools/scan_space_signal.py --peak-tm-min 0.05 --rss-delta-min 5.0 --min-ms 1.0 --max-cv 0.10
```

环境：只用标准库 + `psutil`（`peak_rss` 需要）。Python 3.10+，建议 `C:\Python313\python.exe`。
**必须在装了 `psutil` 的环境跑**，否则 H2 那列为空。

输出（`--out` 的 JSONL，每行一题，边跑边写）：

```json
{"problem_idx": 22, "task_name": "Generate Parentheses", "status": "ok",
 "peak_tm_mb": 4.7071, "peak_rss_mb": 35.9, "rss_delta_mb": 15.38,
 "baseline_ms": 18.427, "cv": 0.046, "reps": 271,
 "pass_h1_space": true, "pass_h2_mu": true, "pass_h3_time": true,
 "protocol_version": "proto-2026.11", "measured_at": "2026-11-27T21:58:03"}
```

失败题只写 `status`/`error`（`status != "ok"` 一律淘汰，不参与通过率）。
stdout 汇总会给出 H1 / H2 / H3' / H1∩H3' 四个通过率；**样本 < 200 题时脚本会自己标「试测，不可外推」**。

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

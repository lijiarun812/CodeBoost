### 表 1  TMU（积分）与时间：同一批题、同一份 final_code

| 模型 | 题数(成功/总数) | TMU（本实现近似）ᵃ(均值/中位) | 中期加权 积分ᵇ(均值) | tracemalloc 峰值ᶜ(均值/中位) | 时间 论文口径 | 时间 新口径 |
|---|---|---|---|---|---|---|
| deepseek-v4-flash-0731（repro_dual） | 19/20 | +23.1% / +3.2% | — | -6.6% / +0.0% | +21.4% | +14.9% |
| deepseek-v4-flash-0731（repro_dual_postfix） | 17/20 | +18.8% / +4.3% | — | -33.3% / +0.0% | +16.0% | +11.6% |

> ᵃ **TMU（本实现近似）**：`mem_mbs = ∫RSS dt ≈ 峰值 × 耗时`，是**耗时代理**，不是空间指标；其「降幅」实为耗时降幅的投影（证据见 `eval/check_mem_metric.py`）。论文的 TMU 绝对值与我们不同口径（论文 GPT-3.5 的 `TMU÷ET = 437 MB` 比其 MU 91 MB 还大 4.8 倍），**绝对值不可比，只能比降幅**。
> ᵇ 中期加权判据同样建立在积分上，故同一问题。
> ᶜ `peak_tracemalloc_mb` 只覆盖 **Python 堆**（不含 C 扩展/numpy），且本数据集题目偏小，分辨力见表 4；进程级空间口径见 **表 1b 的 MU**。

### 表 1b  MU（峰值内存，对齐论文 Table 1 的 MU 列）

| 模型 | 题数 | MU = peak_rss 均值/中位降幅 | MU − 空载基线 均值降幅 | 基线 RSS | 可分辨题数 |
|---|---|---|---|---|---|
| deepseek-v4-flash-0731（repro_dual） | 19 | -0.0% / +0.0% | n/a（无 ≥5 MB 的题） | 20.5 MB | 0/19 |
| deepseek-v4-flash-0731（repro_dual_postfix） | 15 | -0.1% / +0.0% | n/a（无 ≥5 MB 的题） | 20.5 MB | 0/15 |

> 「MU − 空载基线」= `max(0, peak_rss − baseline)`；只有该值 ≥ 5 MB 的题才算**可分辨**（RSS 采样噪声约 ±0.5 MB，小题扣完基线只剩噪声）。

### 表 2  「假 100%」清查

| 模型 | TMU 口径记 100% 的题 | 新口径能给出有限值的题 | 说明 |
|---|---|---|---|
| deepseek-v4-flash-0731（repro_dual） | 0 | 19 | 积分近似在 final 上塌成 0；但这是积分实现的问题，不代表真实空间占用降低 100% |
| deepseek-v4-flash-0731（repro_dual_postfix） | 0 | 17 | 积分近似在 final 上塌成 0；但这是积分实现的问题，不代表真实空间占用降低 100% |

### 表 3  失败原因分布（新口径未测到 = 失败，不计入降幅）

| 模型 | 失败/总数 | 主要原因 |
|---|---|---|
| deepseek-v4-flash-0731（repro_dual） | 1/20 | AssertionError ×1 |
| deepseek-v4-flash-0731（repro_dual_postfix） | 3/20 | AssertionError ×3 |

### 表 4  新口径内存列的分辨力（能分开 baseline 与 final 吗）

| 模型 | baseline 峰值中位 | peak 完全相同的题 | 说明 |
|---|---|---|---|
| deepseek-v4-flash-0731（repro_dual） | 0.0010 MB | 17/19 | 峰值只有亚 KB 级、题目太小，内存列**没有分辨力** |
| deepseek-v4-flash-0731（repro_dual_postfix） | 0.0010 MB | 12/15 | 峰值只有亚 KB 级、题目太小，内存列**没有分辨力** |

### 表 5  「TMU 是不是时间的投影」——逐题配对相关

| 模型 | r(TMU降幅, 时间降幅) | r(MU−基线降幅, 时间降幅) | 判读 |
|---|---|---|---|
| deepseek-v4-flash-0731（repro_dual） | 0.936 | 0.378 | TMU 与时间高度共线 → 不能当空间指标 |
| deepseek-v4-flash-0731（repro_dual_postfix） | 0.967 | -0.198 | TMU 与时间高度共线 → 不能当空间指标 |

新口径基线 CV：中位 6.6%，范围 2.9%–15.7%（34 题），噪声地板 1+2CV ≈ 1.13×。

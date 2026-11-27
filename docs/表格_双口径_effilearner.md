### 表 1  同一批题、同一份 final_code 下的三列降幅

| 模型 | 题数(成功/总数) | 论文口径 积分ᵃ(均值/中位) | 中期加权 积分ᵇ(均值) | 新口径 峰值ᶜ(均值/中位) | 论文口径 时间 | 新口径 时间 |
|---|---|---|---|---|---|---|
| deepseek-v4-flash-0731（repro_dual） | 19/20 | +23.1% / +3.2% | — | -6.6% / +0.0% | +21.4% | +14.9% |
| deepseek-v4-flash-0731（repro_dual_postfix） | 17/20 | +18.8% / +4.3% | — | -33.3% / +0.0% | +16.0% | +11.6% |

> ᵃ 论文口径积分 `mem_mbs = ∫RSS dt ≈ 峰值 × 耗时`，是耗时代理，**不是空间指标**；其「内存降幅」实为耗时降幅的投影（证据见 `eval/check_mem_metric.py`）。
> ᵇ 中期加权判据同样建立在积分上，故同一问题。
> ᶜ 新口径峰值为 `peak_tracemalloc_mb`，是本项目唯一独立的空间口径；但本品数据集题目偏小，分辨力见表 4。

### 表 2  「假 100%」清查

| 模型 | 论文口径记 100% 的题 | 新口径能给出有限值的题 | 说明 |
|---|---|---|---|
| deepseek-v4-flash-0731（repro_dual） | 0 | 19 | 旧口径积分近似在 final 上塌成 0；新口径用 tracemalloc 峰值，不会塌 0 |
| deepseek-v4-flash-0731（repro_dual_postfix） | 0 | 17 | 旧口径积分近似在 final 上塌成 0；新口径用 tracemalloc 峰值，不会塌 0 |

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

新口径基线 CV：中位 6.6%，范围 2.9%–15.7%（34 题），噪声地板 1+2CV ≈ 1.13×。

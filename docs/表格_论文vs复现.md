# 论文 vs 复现：可直接上 PPT 的对照表

> 由 `eval/make_table.py` 生成；论文数字全部来自 `eval/paper_numbers.py`（附 PDF 页码），我们的数字来自 `results/`。

**评测基准**：EffiBench: Benchmarking the Efficiency of Automatically Generated Code（NeurIPS 2024 Datasets & Benchmarks）（1000 题 / 评测 42 个模型）。GPT-4 生成代码平均是 canonical solution 的 3.12 倍耗时，极端情况 13.89 倍耗时、43.92 倍总内存

> 出处：doc/EffiBench.pdf 摘要（第 1 页）

## 一、EffiLearner：论文 vs 复现

**论文结果**（doc/EffiLearner.pdf：Table 1（第 6 页）+ §4.3 Main Results 正文）

| 模型 | 指标 | 初始 | 优化后 | 论文报告下降 |
|---|---|---|---|---|
| StarCoder2-15B | ET（执行时间 (s)） | 0.93 | 0.12 | -87.1% |
| StarCoder2-15B | TMU（总内存占用，内存-时间积分 (MB*s)） | 22.02 | 2.03 | -90.8% |
| GPT-3.5-Turbo-0301 | ET（执行时间 (s)） | 0.36 | 0.28 | -22.2% |
| GPT-3.5-Turbo-0301 | MU（最大内存占用 (MB)） | 91.25 | 36.08 | -60.5% |
| GPT-3.5-Turbo-0301 | TMU（总内存占用，内存-时间积分 (MB*s)） | 157.5 | 12.43 | -92.1% |
| GPT-4 | ET（执行时间 (s)） | 0.31 | 0.28 | -9.7% |
| GPT-4 | MU（最大内存占用 (MB)） | 80.88 | 63.82 | -21.1% |
| DeepSeek-6.7B-Instruct | ET（执行时间 (s)） | 0.37 | 0.34 | -8.1% |
| DeepSeek-6.7B-Instruct | TMU（总内存占用，内存-时间积分 (MB*s)） | 555.18 | 13.66 | -97.5% |

**我们的复现**（EffiBench 前 100 题 × 3 轮，API 调用）

| 模型 | 成功题 | 时间下降 | 内存下降（旧口径 mem_mbs） | 内存下降（归一化 + 剔除零积分题） | 零积分题 |
|---|---|---|---|---|---|
| deepseek-flash | 87/100 | 27.5% | 36.3% | 8.9% | 27 |
| deepseek-v4-pro | 80/100 | 23.9% | 33.7% | 10.2% | 28 |
| glm-4.6 | 82/100 | 18.3% | 26.0% | 8.4% | 24 |
| qwen3-coder-flash | 79/100 | 15.6% | 24.5% | 9.9% | 22 |

> 口径说明：`mem_mbs` 是 RSS-时间积分（≈平均常驻内存 × 执行时间），代码变快本身就会让它下降；「归一化 + 剔除零积分题」= 先用 `mem_mbs/exec_time` 去掉时间因素，再剔掉积分恒为 0 的题（采样点不足），剩下的才是真正的内存收益。三者近似满足 `1-(1-可测口径)(1-时间下降) ≈ 旧口径`，可用数字自检。
> 结论方向与论文一致（带开销反馈的迭代能降低时间/内存），但**绝对幅度不可直接比较**：模型、题目规模、测量方式都不同。

## 二、PerfCodeGen：论文 vs 复现

**论文结果**（doc/PerfCodeGen.pdf 摘要（第 1 页）+ 官方仓库 README「Key result」）

| 项 | 论文 | 我们 |
|---|---|---|
| GPT-4 上被优化到优于 ground truth 的题目比例（HumanEval） | ~47% | 见下表 |
| 同上（MBPP） | ~56% | 本次未跑 MBPP |

**我们的复现**

| 模型 | 题目数 | %Correct | Speedup(avg) | %Opt(avg) | Best@5 | 最终版 Speedup（vs base） | 最终版 %Opt（vs base） | 剔除极端 |
|---|---|---|---|---|---|---|---|---|
| deepseek-flash | 1 | 1.0 | 0.0 | 0.0 | 0.0 | 2.0 | 1 | 0 |

> 反馈策略：feedback；采样数：1；测量相对 MAD 中位数：None

## 三、与论文设置的差异（这一节答辩必问）

| 维度 | 论文 | 我们 |
|---|---|---|
| 模型 | GPT-4 / StarCoder2 / DeepSeek-6.7B 等 22 个 | deepseek-flash、deepseek-v4-pro、glm-4.6、qwen3-coder-flash |
| 题目集 | EffiBench（1000 题）/ HumanEval / MBPP | EffiBench 前 100 题（class Solution 题型） |
| 采样数 | PerfCodeGen sample_num=20；EffiLearner 单样本迭代 | base 采样默认 1（可调 --samples），另给 Best@k |
| 执行环境 | 集群多 CPU  pod + 并行评测 + 官方 testcase 输入 | 单机子进程沙盒；用例按 `def check(candidate)` 的 assert 切分 |
| 时间测量 | 单次 wall clock（集群） | 重复执行放大到 ~0.25s + 多轮取最小值 + 报告 MAD |
| 内存测量 | 官方 profiler 的内存-时间积分（MB*s） | 同时给积分与归一化等效常驻内存，并剔除零积分题 |
| 优化采纳判据 | 低于原版本即采纳（按论文提示词） | Pareto：时间至少快 5%，且内存不劣化 >5% |


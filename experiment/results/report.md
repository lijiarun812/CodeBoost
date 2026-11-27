# 执行反馈粒度对 LLM 代码性能优化影响 — 实验报告

## 0. 关键发现

- 模型：本地 Qwen2.5-Coder-7B（Ollama），数据集：HumanEval 子集 12 题。
- Base 阶段 LLM 写出正确代码的题数：9/12。
- vanilla: 真正优化成功 1 题，破坏正确性 1 题。
- perfcodegen: 真正优化成功 2 题，破坏正确性 1 题。
- ours: 真正优化成功 1 题，破坏正确性 1 题。
- 主要现象：Qwen2.5-Coder-7B 在 base 阶段已直接写出迭代/公式/筛法实现，
  大多数题基线耗时已在亚毫秒~毫秒级，PerfCodeGen 式执行反馈的边际收益很小；
  唯一显著优化案例是 HumanEval/40（三元组求和），三组方法都拿到 ~2x speedup；
  HumanEval/147（三元组计数）朴素 O(n³) 跑 1067ms，但 LLM 尝试用数学公式优化时全部写错（失败案例 C）。

## 1. 逐题结果（最慢档输入耗时 ms）

| Task | entry | Base ms | Vanilla ms (sp) | PerfCodeGen ms (sp) | Ours ms (sp) |
|---|---|---|---|---|---|---|
| HumanEval/31 | is_prime | 0.122 | 0.11 (1.09x) | 0.11 (1.07x) | 0.11 (1.09x) |
| HumanEval/55 | fib | 0.001 | 0.00 (1.0x) | 0.00 (1.0x) | 0.00 (1.0x) |
| HumanEval/59 | largest_prime_factor | 1.524 | 1.31 (1.17x) | 1.27 (1.2x) | 1.35 (1.13x) |
| HumanEval/60 | sum_to_n | 0.000 | 0.00 (Nonex) | 0.00 (Nonex) | 0.00 (Nonex) |
| HumanEval/63 | fibfib | 0.001 | 0.00 (1.0x) | 0.00 (1.0x) | 0.00 (1.0x) |
| HumanEval/96 | count_up_to | 72.739 | 70.44 (1.03x) | 72.61 (1.0x) | 77.58 (0.94x) |
| HumanEval/123 | get_odd_collatz | 0.028 | 0.02 (1.17x) | 0.02 (1.47x) | 0.03 (1.04x) |
| HumanEval/130 | tri | BASE_WRONG | — | — | — |
| HumanEval/139 | special_factorial | BASE_WRONG | — | — | — |
| HumanEval/147 | get_max_triples | 1067.828 | ❌wrong | ❌wrong | ❌wrong |
| HumanEval/40 | triples_sum_to_zero | 0.145 | 0.07 (2.07x) | 0.06 (2.38x) | 0.06 (2.42x) |
| HumanEval/120 | maximum | BASE_WRONG | — | — | — |

## 2. 汇总

- **vanilla**: avg speedup=1.22x, 显著变快(≥1.3x)=1, 变慢(<0.95x)=0, 破坏正确性=1
- **perfcodegen**: avg speedup=1.3x, 显著变快(≥1.3x)=2, 变慢(<0.95x)=0, 破坏正确性=1
- **ours**: avg speedup=1.23x, 显著变快(≥1.3x)=1, 变慢(<0.95x)=1, 破坏正确性=1

## 3. 失败案例分类（对齐大作业 docx A-E）


### A. 原始代码已接近最优（3）
- HumanEval/59 [vanilla]: 基线已接近最优，speedup 1.17x
- HumanEval/59 [perfcodegen]: 基线已接近最优，speedup 1.2x
- HumanEval/59 [ours]: 基线已接近最优，speedup 1.13x

### B. LLM 找错瓶颈（1）
- HumanEval/96 [ours]: 反而变慢 0.94x，LLM 改坏了

### C. 优化后更快但破坏正确性（3）
- HumanEval/147 [vanilla]: 优化后未通过 HumanEval check()
- HumanEval/147 [perfcodegen]: 优化后未通过 HumanEval check()
- HumanEval/147 [ours]: 优化后未通过 HumanEval check()

### D. 算法没变/微改（2）
- HumanEval/96 [vanilla]: speedup 1.03x，算法层面无实质改动
- HumanEval/96 [perfcodegen]: speedup 1.0x，算法层面无实质改动

### E. 计时噪声（15）
- HumanEval/31 [vanilla]: 基线仅 0.122ms，speedup 1.09x 在噪声范围内
- HumanEval/31 [perfcodegen]: 基线仅 0.122ms，speedup 1.07x 在噪声范围内
- HumanEval/31 [ours]: 基线仅 0.122ms，speedup 1.09x 在噪声范围内
- HumanEval/55 [vanilla]: 基线仅 0.001ms，speedup 1.0x 在噪声范围内
- HumanEval/55 [perfcodegen]: 基线仅 0.001ms，speedup 1.0x 在噪声范围内
- HumanEval/55 [ours]: 基线仅 0.001ms，speedup 1.0x 在噪声范围内
- HumanEval/63 [vanilla]: 基线仅 0.001ms，speedup 1.0x 在噪声范围内
- HumanEval/63 [perfcodegen]: 基线仅 0.001ms，speedup 1.0x 在噪声范围内
- HumanEval/63 [ours]: 基线仅 0.001ms，speedup 1.0x 在噪声范围内
- HumanEval/123 [vanilla]: 基线仅 0.028ms，speedup 1.17x 在噪声范围内
- HumanEval/123 [perfcodegen]: 基线仅 0.028ms，speedup 1.47x 在噪声范围内
- HumanEval/123 [ours]: 基线仅 0.028ms，speedup 1.04x 在噪声范围内
- HumanEval/40 [vanilla]: 基线仅 0.145ms，speedup 2.07x 在噪声范围内
- HumanEval/40 [perfcodegen]: 基线仅 0.145ms，speedup 2.38x 在噪声范围内
- HumanEval/40 [ours]: 基线仅 0.145ms，speedup 2.42x 在噪声范围内